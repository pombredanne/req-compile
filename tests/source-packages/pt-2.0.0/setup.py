#!/usr/bin/env python
# setup.py: Setup information.
# Copyright (C) 2010 Yesudeep Mangalapilly <yesudeep@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import os
import imp
from setuptools import setup

PKG_DIR = 'pathtools'
version = imp.load_source('version',
                          os.path.join(PKG_DIR, 'version.py'))

def read_file(filename):
    """
    Reads the contents of a given file relative to the directory
    containing this file and returns it.

    :param filename:
        The file to open and read contents from.
    """
    return open(os.path.join(os.path.dirname(__file__), filename)).read()

setup(name='pt',
      version=version.VERSION_STRING,
      long_description=read_file('README'),
      )
