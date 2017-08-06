#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Get your Spotify folder hierarchy with playlists into JSON.

    :license: MIT, see LICENSE for more details.
"""
from __future__ import print_function

import argparse
import json
try:
    from urllib import unquote_plus  # Python 2
except ImportError:
    from urllib.parse import unquote_plus  # Python 3
import re
import subprocess
import os


# Change this if different on your machine.
PERSISTENT_CACHE_PATH = (
    '$HOME/Library/Application Support/Spotify/PersistentCache/Storage')


def parse(file_name, user_id):
    """
    Parse a Spotify PersistantStorage file with folder structure at start.

    `file_name`
        Location of a PersistantStorage file.
    `user_id`
        Specify a user id to use for folder URIs. Can also be a
        placeholder value like 'unknown'. (Background: this information
        doesn't seem to be provided in the source file.)
    """
    with open(file_name, 'rb') as data_file:
        data = data_file.read()
    rows = re.split(b'spotify:[use]', data)
    folder = {'type': 'folder', 'children': []}
    stack = []
    for row in rows:
        # Note: '\r' marks end of repeated block. This might break in
        # future versions of Spotify. An alternative solution is to read
        # the number of repeats coded into the protobuf file.
        chunks = row.split(b'\r', 1)
        row = chunks[0]
        if row.startswith(b'ser:'):
            folder['children'].append({
                'type': 'playlist',
                'uri': 'spotify:u' + row[:-1].decode('utf-8')})
        elif row.startswith(b'tart-group:'):
            stack.append(folder)
            folder = {'type': 'folder', 'children': []}
            tags = row.split(b':')
            # Assuming folder names < 128 characters.
            # Alternatively, do a protobuf varint parser to get length.
            folder['name'] = unquote_plus(tags[-1][:-1].decode('utf-8'))
            folder['uri'] = (
                ('spotify:user:%s:folder:' % user_id)
                + tags[-2].decode('utf-8'))
        elif row.startswith(b'nd-group:'):
            parent = stack.pop()
            parent['children'].append(folder)
            folder = parent
        if folder.get('children') and len(chunks) > 1:
            break
    return folder


def get_folder(folder_id, data):
    """Get data for a particular folder in parsed data."""
    data_type = data.get('type')
    if data_type == 'folder':
        if data.get('uri', '').endswith(folder_id):
            return data
        for child in data.get('children', []):
            folder = get_folder(folder_id, child)
            if folder:
                return folder


def get_newest_persistent_cache_file(path):
    """Get newest file in PersistentCache storage with "start-group" marker."""
    path = path.replace('$HOME', os.getenv('HOME'))
    return subprocess.check_output((
        'grep -rl "start-group" "{path}" --null '
        '| xargs -0 ls -t | head -1'.format(path=path)),
        shell=True).strip()


def _process(file_name, args, user_id='unknown'):
    # preprocessing
    if args.folder:
        uri = args.folder
        if '/' not in uri and ':' not in uri:
            print('Specify folder as a URL or Spotify URI. See `--help`.')
            return
        separator = '/' if uri.find('/') > 0 else ':'
        user_id = uri.split(separator)[-3]
        folder_id = uri.split(separator)[-1]
    data = parse(file_name, user_id=user_id)
    # postprocessing
    if args.folder:
        data = get_folder(folder_id, data)
        if not data:
            print('Folder not found :(')
            return
    return json.dumps(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=(
            'Get your Spotify folder hierarchy with playlists into JSON.'),
        add_help=False)
    parser.add_argument(
        'folder', default=None, metavar='folder', nargs='?',
        help=('Get only a specific Spotify folder. If omitted, returns entire '
              'hierarchy. A folder is specified by its URL or URI. '
              'Obtain this by dragging a folder into a Terminal window. '
              'Alternatively, click on a folder in Spotify and do Cmd+C.'))
    parser.add_argument(
        '--cache', dest='cache_dir', default=PERSISTENT_CACHE_PATH,
        help='Specify a custom PersistentCache directory to look for data in.')
    parser.add_argument(
        '-h', '--help', action='help', default=argparse.SUPPRESS,
        help='Show this help message and exit.')

    args = parser.parse_args()
    cache_file_name = get_newest_persistent_cache_file(args.cache_dir)
    if cache_file_name:
        print(_process(cache_file_name, args))
    else:
        print('No data found in Spotify cache. If you have a custom cache '
              'directory set, specify its path with the `--cache` flag.')
