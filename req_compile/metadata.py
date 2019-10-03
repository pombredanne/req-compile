from __future__ import print_function

import contextlib
from contextlib import closing
import shutil
import subprocess
import imp
import io
import logging
import os
import sys
import tempfile
import zipfile
import functools
from types import ModuleType

import setuptools
import six
from six.moves import StringIO, configparser
import pkg_resources

from req_compile import utils
from req_compile.dists import DistInfo
from req_compile.extractor import NonExtractor, TarExtractor, ZipExtractor
from req_compile.importhook import import_hook, import_contents, remove_encoding_lines

LOG = logging.getLogger('req_compile.metadata')

FAILED_BUILDS = set()


class MetadataError(Exception):
    def __init__(self, name, version, ex):
        super(MetadataError, self).__init__()
        self.name = name
        self.version = version
        self.ex = ex

    def __str__(self):
        return 'Failed to parse metadata for package {} ({}) - {}: {}'.format(
            self.name, self.version, self.ex.__class__.__name__, str(self.ex))


def parse_source_filename(full_filename):
    filename = full_filename
    filename = filename.replace('.tar.gz', '')
    filename = filename.replace('.tar.bz2', '')
    filename = filename.replace('.zip', '')
    filename = filename.replace('.tgz', '')

    dash_parts = filename.split('-')
    version_start = None
    for idx, part in enumerate(dash_parts):
        if idx != 0 and (part[0].isdigit() or
                         (len(part) > 1 and part[0].lower() == 'v' and part[1].isdigit())):
            if idx != len(dash_parts) - 1 and '.' in dash_parts[idx + 1] and '.' not in dash_parts[idx]:
                continue
            version_start = idx
            break

    if version_start is None:
        return os.path.basename(full_filename), None

    if version_start == 0:
        raise ValueError('Package name missing: {}'.format(full_filename))

    pkg_name = '-'.join(dash_parts[:version_start])

    version_str = '-'.join(dash_parts[version_start:]).replace('_', '-')
    version_parts = version_str.split('.')
    for idx, part in enumerate(version_parts):
        if idx != 0 and (part.startswith('linux') or
                         part.startswith('windows') or
                         part.startswith('macos')):
            version_parts = version_parts[:idx]
            break

    version = utils.parse_version('.'.join(version_parts))
    return pkg_name, version


def extract_metadata(filename, run_setup_py=True, origin=None):
    """Extract a DistInfo from a file or directory

    Args:
        filename (str): File or path to extract metadata from
        origin (str, req_compile.repos.Repository: Origin of the metadata

    Returns:
        (RequirementContainer) the result of the metadata extraction
    """
    LOG.info('Extracting metadata for %s', filename)
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext == '.whl':
        LOG.debug('Extracting from wheel')
        try:
            result = _fetch_from_wheel(filename)
        except zipfile.BadZipfile as ex:
            raise MetadataError(os.path.basename(filename).replace('.whl', ''), '0.0', ex)
    elif ext == '.zip':
        LOG.debug('Extracting from a zipped source package')
        result = _fetch_from_source(filename, ZipExtractor, run_setup_py=run_setup_py)
    elif ext in ('.gz', '.bz2', '.tgz'):
        LOG.debug('Extracting from a tar package')
        if ext == '.tgz':
            ext = 'gz'
        result = _fetch_from_source(os.path.abspath(filename),
                                    functools.partial(TarExtractor, ext.replace('.', '')),
                                    run_setup_py=run_setup_py)
    elif ext in ('.egg',):
        LOG.debug('Attempted to resolve an unsupported format')
        return None
    else:
        LOG.debug('Extracting directly from a source directory')
        result = _fetch_from_source(os.path.abspath(filename), NonExtractor, run_setup_py=run_setup_py)

    if result is not None:
        result.origin = origin
    return result


def find_in_archive(extractor, filename, max_depth=None):
    for info_name in extractor.names():
        if info_name.lower().endswith(filename) and (max_depth is None or info_name.count('/') <= max_depth):
            if '/' not in filename and info_name.rsplit('/')[-1] != filename:
                continue
            return info_name
    return None


def _fetch_from_source(source_file, extractor_type, run_setup_py=True):
    """

    Args:
        source_file (str): Source file
        extractor_type (type[Extractor]): Type of extractor to use

    Returns:

    """
    if not os.path.exists(source_file):
        raise ValueError('Source file/path {} does not exist'.format(source_file))

    name, version = parse_source_filename(os.path.basename(source_file))

    if source_file in FAILED_BUILDS:
        raise MetadataError(name, version, Exception('Build has already failed before'))

    extractor = extractor_type(source_file, source_file)
    with closing(extractor):
        if run_setup_py:
            LOG.info('Attempting to fetch metadata from setup.py')
            results = _fetch_from_setup_py(source_file, name, version, extractor)
            if results is not None:
                return results
        else:
            extractor.fake_root = None

        metadata_file = find_in_archive(extractor, 'metadata', max_depth=1)
        if metadata_file is not None:
            try:
                LOG.info('Attempting to fetch metadata from %s', metadata_file)
                return _parse_flat_metadata(extractor.open(metadata_file, encoding='utf-8').read())
            except IOError:
                LOG.warning('Could not parse metadata file %s', metadata_file)

        egg_requires_file = find_in_archive(extractor, '.egg-info/requires.txt')
        if egg_requires_file is not None:
            LOG.info('Attempting to fetch metadata from %s', egg_requires_file)
            try:
                requires_contents = extractor.open(egg_requires_file, encoding='utf-8').read()
                return _parse_requires_file(requires_contents,
                                            name,
                                            version)
            except IOError:
                LOG.warning('Failed to load requires.txt')

        pkg_info_file = find_in_archive(extractor, '.egg-info/pkg-info')
        if pkg_info_file is not None:
            try:
                LOG.info('Attempting to fetch metadata from %s', pkg_info_file)
                return _parse_flat_metadata(extractor.open(pkg_info_file, encoding='utf-8').read())
            except IOError:
                LOG.warning('Could not parse metadata file %s', pkg_info_file)

        LOG.warning('No metadata source could be found for the source dist %s', source_file)
        FAILED_BUILDS.add(source_file)
        raise MetadataError(name, version, Exception('Invalid project distribution'))


def _fetch_from_setup_py(source_file, name, version, extractor):  # pylint: disable=too-many-branches
    """Attempt a set of executions to obtain metadata from the setup.py without having to build
    a wheel.  First attempt without mocking __import__ at all. This means that projects
    which import a package inside of themselves will not succeed, but all other simple
    source distributions will. If this fails, allow mocking of __import__ to extract from
    tar files and zip files.  Imports will trigger files to be extracted and executed.  If
    this fails, due to true build prerequisites not being satisfied or the mocks being
    insufficient, build the wheel and extract the metadata from it.

    Args:
        source_file (str): The source archive or directory
        name (str): The project name. Use if it cannot be determined from the archive
        extractor (Extractor): The extractor to use to obtain files from the archive

    Returns:
        (DistInfo) The resulting distribution metadata
    """
    setup_file = find_in_archive(extractor, 'setup.py', max_depth=1)

    if name == 'setuptools':
        LOG.debug('Not running setup.py for setuptools')
        return None

    if setup_file is None:
        LOG.warning('Could not find a setup.py in %s', os.path.basename(source_file))
        return None

    fake_setupdir = tempfile.mkdtemp(suffix='_{}_{}'.format(extractor.__class__.__name__, name))
    LOG.debug('Setting root to %s', fake_setupdir)
    extractor.fake_root = fake_setupdir

    results = None
    try:
        LOG.info('Parsing setup.py %s', setup_file)
        results = _parse_setup_py(name, fake_setupdir, setup_file, extractor, False)
    except Exception:  # pylint: disable=broad-except
        LOG.warning('Failed to parse %s without import mocks', name, exc_info=True)
        try:
            LOG.info('Parsing setup.py %s with archive mocks', setup_file)
            results = _parse_setup_py(name, fake_setupdir, setup_file, extractor, True)
        except (Exception, RuntimeError, ImportError):  # pylint: disable=broad-except
            LOG.warning('Failed to parse %s with import mocks', name, exc_info=True)

            # results = _build_wheel(name, source_file)
            results = _build_egg_info(name, extractor, setup_file)
    finally:
        if fake_setupdir != source_file:
            shutil.rmtree(fake_setupdir)

    if results is None or (results.name is None and results.version is None):
        return None

    if results.name is None:
        results.name = name
    if results.version is None or (version and results.version != version):
        results.version = version or utils.parse_version('0.0.0')
    return results


def _build_wheel(name, source_file):
    results = None
    temp_wheeldir = tempfile.mkdtemp()
    LOG.info('Building wheel file for %s', source_file)
    try:
        subprocess.check_output([
            sys.executable,
            '-m', 'pip', 'wheel',
            source_file, '--no-deps', '--wheel-dir', temp_wheeldir
        ], stderr=subprocess.STDOUT, universal_newlines=True)
        wheel_file = os.path.join(temp_wheeldir, os.listdir(temp_wheeldir)[0])
        results = _fetch_from_wheel(wheel_file)
    except subprocess.CalledProcessError as ex:
        LOG.warning('Failed to build wheel for %s: %s', name, ex.output, exc_info=True)
    finally:
        shutil.rmtree(temp_wheeldir)
    return results


def _build_egg_info(name, extractor, setup_file):
    results = None
    temp_tar = tempfile.mkdtemp()

    extractor.extract(temp_tar)

    extracted_setup_py = os.path.join(temp_tar, setup_file)
    LOG.info('Building egg info for %s [%s]', extracted_setup_py, list(os.listdir(temp_tar)))
    try:
        subprocess.check_output([
            sys.executable, extracted_setup_py, 'egg_info'
        ], cwd=os.path.dirname(extracted_setup_py), stderr=subprocess.STDOUT, universal_newlines=True)
        LOG.info('Build egg info successfully: [%s]',  list(os.listdir(os.path.dirname(extracted_setup_py))))
        return extract_metadata(os.path.dirname(extracted_setup_py), run_setup_py=False)
    except subprocess.CalledProcessError as ex:
        LOG.warning('Failed to build egg-info for %s: %s', name, ex.output, exc_info=True)
        return _build_wheel(name, os.path.dirname(extracted_setup_py))
    finally:
        shutil.rmtree(temp_tar)
    return results


def _fetch_from_wheel(wheel):
    zfile = zipfile.ZipFile(wheel, 'r')
    with closing(zfile):
        metadata_file = None
        infos = zfile.namelist()
        for info in infos:
            if info.endswith('.dist-info/METADATA'):
                metadata_file = info
                break

        if metadata_file:
            return _parse_flat_metadata(zfile.read(metadata_file).decode('utf-8', 'ignore'))

        return None


def _parse_flat_metadata(contents):
    name = None
    version = None
    raw_reqs = []

    for line in contents.split('\n'):
        lower_line = line.lower()
        if name is None and lower_line.startswith('name:'):
            name = line.split(':')[1].strip()
        elif version is None and lower_line.startswith('version:'):
            version = utils.parse_version(line.split(':')[1].strip())
        elif lower_line.startswith('requires-dist:'):
            raw_reqs.append(line.split(':')[1].strip())

    return DistInfo(name, version, list(utils.parse_requirements(raw_reqs)))


def parse_req_with_marker(req_str, marker):
    return utils.parse_requirement(req_str + ' and {}'.format(marker) if ';' in req_str else
                                   req_str + '; {}'.format(marker))


def setup(results, *args, **kwargs):
    # pbr uses a dangerous pattern that only works when you build using setuptools
    if 'pbr' in kwargs:
        raise ValueError('Must build wheel if pbr is used')

    if (not args and not kwargs) or ('name' not in kwargs and os.path.exists('setup.cfg')):
        name, version, reqs, extra_reqs = _parse_setup_cfg(**kwargs)
    else:
        name = kwargs.get('name', None)
        version = kwargs.get('version', None)
        reqs = kwargs.get('install_requires', [])
        extra_reqs = kwargs.get('extras_require', {})

    if version is not None:
        version = pkg_resources.parse_version(str(version))

    if isinstance(reqs, str):
        reqs = [reqs]
    all_reqs = list(utils.parse_requirements(reqs))
    for extra, extra_req_strs in extra_reqs.items():
        extra = extra.strip()
        if not extra:
            continue
        try:
            if isinstance(extra_req_strs, six.string_types):
                extra_req_strs = [extra_req_strs]
            cur_reqs = utils.parse_requirements(extra_req_strs)
            if extra.startswith(':'):
                req_with_marker = [
                    parse_req_with_marker(str(cur_req), extra[1:])
                    for cur_req in cur_reqs]
            else:
                req_with_marker = [
                    parse_req_with_marker(str(cur_req), 'extra=="{}"'.format(extra))
                    for cur_req in cur_reqs]
            all_reqs.extend(req_with_marker)
        except pkg_resources.RequirementParseError as ex:
            print('Failed to parse extra requirement ({}) '
                  'from the set:\n{}'.format(str(ex), extra_reqs), file=sys.stderr)
            raise

    if name is not None:
        name = name.replace(' ', '-')
    results.append(DistInfo(name, version, all_reqs))

    # Some projects inspect the setup() result
    class FakeResult(object):
        def __getattr__(self, item):
            return None
    return FakeResult()


def begin_patch(module, member, new_value):
    if isinstance(module, str):
        if module not in sys.modules:
            return None

        module = sys.modules[module]

    if not hasattr(module, member):
        old_member = None
    else:
        old_member = getattr(module, member)
    setattr(module, member, new_value)
    return module, member, old_member


def end_patch(token):
    if token is None:
        return

    module, member, old_member = token
    if old_member is None:
        delattr(module, member)
    else:
        setattr(module, member, old_member)


@contextlib.contextmanager
def patch(*args):
    """Manager a patch in a contextmanager"""
    tokens = []
    for idx in range(0, len(args), 3):
        module, member, new_value = args[idx:idx + 3]
        tokens.append(begin_patch(module, member, new_value))
        
    try:
        yield
    finally:
        for token in tokens[::-1]:
            end_patch(token)


def _get_include():
    return ''


class FakeNumpyModule(ModuleType):
    """A module simulating numpy"""
    def __init__(self, name):
        ModuleType.__init__(self, name)  # pylint: disable=non-parent-init-called,no-member
        self.__version__ = '2.16.0'
        self.get_include = _get_include


class FakeModule(ModuleType):
    """A module simulating cython"""
    def __init__(self, name):
        ModuleType.__init__(self, name)  # pylint: disable=non-parent-init-called,no-member

    def __call__(self, *args, **kwargs):
        return FakeModule('')

    def __iter__(self):
        return iter([])

    def __getattr__(self, item):
        if item == '__path__':
            return []
        if item == 'setup':
            return setuptools.setup
        return FakeModule(item)


def _parse_setup_cfg(**kwargs):
    LOG.info('Parsing from setup.cfg')

    parser = configparser.ConfigParser()
    parser.read('setup.cfg')

    install_requires = kwargs.get('install_requires', [])
    if parser.has_option('options', 'install_requires'):
        install_requires.extend(parser.get('options', 'install_requires').split('\n'))

    extras_require = kwargs.get('extras_require', {})
    if parser.has_section('options.extras_require'):
        for extra, req_str in parser.items('options.extras_require'):
            extras_require[extra] = req_str.split('\n')

    return parser.get('metadata', 'name'), parser.get('metadata', 'version'), install_requires, extras_require


# pylint: disable=too-many-branches
def _parse_setup_py(name, fake_setupdir, setup_file, extractor, mock_import):  # pylint: disable=too-many-locals,too-many-statements
    # pylint: disable=bad-option-value,no-name-in-module,no-member,import-outside-toplevel
    # Capture warnings.warn, which is sometimes used in setup.py files

    logging.captureWarnings(True)

    results = []
    setup_with_results = functools.partial(setup, results)

    import os.path  # pylint: disable=redefined-outer-name

    spy_globals = {'__file__': os.path.join(fake_setupdir, setup_file),
                   '__name__': '__main__',
                   'setup': setup_with_results}

    # pylint: disable=unused-import,unused-variable
    import codecs
    import distutils.core
    import setuptools.extern  # Extern performs some weird module manipulation we can't handle

    # A few package we have trouble importing with the importhook
    import setuptools.command
    import setuptools.command.sdist

    try:
        import importlib.util
    except ImportError:
        pass

    if 'numpy' not in sys.modules:
        sys.modules['numpy'] = FakeNumpyModule('numpy')
        sys.modules['numpy.distutils'] = FakeModule('distutils')
        sys.modules['numpy.distutils.core'] = FakeModule('core')
        sys.modules['numpy.distutils.misc_util'] = FakeModule('misc_util')
        sys.modules['numpy.distutils.system_info'] = FakeModule('system_info')

    old_dir = os.getcwd()

    def _fake_exists(path):
        return extractor.exists(path)

    def _fake_execfile(path):
        exec(extractor.contents(path), spy_globals, spy_globals)

    os.chdir(fake_setupdir)
    orig_chdir = os.chdir

    def _fake_chdir(new_dir):
        if os.path.isabs(new_dir):
            new_dir = os.path.relpath(new_dir, fake_setupdir)
            if new_dir != '.' and new_dir.startswith('.'):
                raise ValueError('Cannot operate outside of setup dir ({})'.format(new_dir))
        try:
            os.mkdir(new_dir)
        except OSError:
            pass
        return orig_chdir(new_dir)

    old_cythonize = None
    try:
        import Cython.Build
        old_cythonize = Cython.Build.cythonize
        Cython.Build.cythonize = lambda *args, **kwargs: ''
    except ImportError:
        sys.modules['Cython'] = FakeModule('Cython')
        sys.modules['Cython.Build'] = FakeModule('Build')
        sys.modules['Cython.Distutils'] = FakeModule('Distutils')
        sys.modules['Cython.Compiler'] = FakeModule('Compiler')
        sys.modules['Cython.Compiler.Main'] = FakeModule('Main')

    def os_error_call(*_args, **_kwargs):
        raise OSError('Popen not permitted')

    if mock_import:
        fake_import = functools.partial(import_hook, extractor.open)

        class FakeSpec(object):  # pylint: disable=too-many-instance-attributes
            class Loader(object):
                def exec_module(self, module):
                    pass

            def __init__(self, modname, path):
                self.loader = FakeSpec.Loader()
                self.name = modname
                self.path = path
                self.submodule_search_locations = None
                self.has_location = True
                self.origin = path
                self.cached = False
                self.parent = None
                self.loader = None

                self.contents = extractor.contents(path)

        # pylint: disable=unused-argument
        def fake_load_source(modname, filename, filehandle=None):
            return import_contents(modname, filename, extractor.contents(filename))

        def fake_spec_from_file_location(modname, path, submodule_search_locations=None):
            return FakeSpec(modname, path)

        def fake_module_from_spec(spec):
            return import_contents(spec.name, spec.path, spec.contents)

        spec_from_file_location_patch = begin_patch('importlib.util',
                                                    'spec_from_file_location', fake_spec_from_file_location)
        module_from_spec_patch = begin_patch('importlib.util',
                                             'module_from_spec', fake_module_from_spec)
        py2_import = begin_patch('builtins', '__import__', fake_import)
        py3_import = begin_patch('__builtin__', '__import__', fake_import)
        load_source_patch = begin_patch(imp, 'load_source', fake_load_source)

    with patch(sys, 'stderr', StringIO(),
               sys, 'stdout', StringIO(),
               os, '_exit', sys.exit,
               os, 'symlink', lambda *_: None,
               'builtins', 'open', extractor.open,
               '__builtin__', 'open', extractor.open,
               '__builtin__', 'execfile', _fake_execfile,
               subprocess, 'check_call', os_error_call,
               subprocess, 'check_output', os_error_call,
               subprocess, 'Popen', os_error_call,
               os, 'listdir', lambda path: [],
               os.path, 'exists', _fake_exists,
               os.path, 'isfile', _fake_exists,
               os, 'chdir', _fake_chdir,
               io, 'open', extractor.open,
               codecs, 'open', extractor.open,
               setuptools, 'setup', setup_with_results,
               distutils.core, 'setup', setup_with_results,
               sys, 'argv', ['setup.py', 'egg_info']):

        try:
            setup_dir = os.path.dirname(setup_file)
            sys.path.insert(0, os.path.abspath(setup_dir))
            if setup_dir:
                os.chdir(setup_dir)

            contents = extractor.contents(os.path.basename(setup_file))
            if six.PY2:
                contents = remove_encoding_lines(contents)

            # pylint: disable=exec-used
            contents = contents.replace('print ', '')
            exec(contents, spy_globals, spy_globals)
        except SystemExit:
            LOG.warning('setup.py raised SystemExit')
        finally:
            if old_cythonize is not None:
                Cython.Build.cythonize = old_cythonize
            if fake_setupdir in sys.path:
                sys.path.remove(fake_setupdir)

            if mock_import:
                end_patch(load_source_patch)
                end_patch(spec_from_file_location_patch)
                end_patch(module_from_spec_patch)
                end_patch(py2_import)
                end_patch(py3_import)

            for module_name in list(sys.modules.keys()):
                module = sys.modules[module_name]
                if module is None:
                    continue
                if 'version' in module_name and module_name not in ('pluggy._version', '_pytest_mock_version', 'pkg_resources.extern.packaging.version', '_pytest._version', 'packaging.version', 'setuptools.version', 'distutils.version', 'funcsigs.version','setuptools.extern.packaging.version', 'py._version', 'pkg_resources._vendor.packaging.version', 'setuptools._vendor.packaging.version'):
                    pass
                if isinstance(module, (FakeModule, FakeNumpyModule)):
                    del sys.modules[module_name]
                elif hasattr(module, '__file__') and extractor.contains_path(module.__file__):
                    del sys.modules[module_name]

            orig_chdir(old_dir)

    if not results:
        raise ValueError('Distutils/setuptools setup() was not ever '
                         'called on "{}". Is this a valid project?'.format(name))
    return results[0]


def _parse_requires_file(contents, name, version):
    reqs = []
    sections = list(pkg_resources.split_sections(contents))
    for section in sections:
        if section[0] is None:
            reqs.extend(utils.parse_requirements(section[1]))
        else:
            extra, _, marker = section[0].partition(':')
            for req in section[1]:
                req = req.strip()
                if not req:
                    continue

                if extra:
                    req = parse_req_with_marker(req, 'extra=="{}"'.format(extra))
                if marker:
                    req = parse_req_with_marker(str(req), marker)
                if req:
                    reqs.append(req)

    return DistInfo(name, version, reqs)
