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
import sys


# Change if different on your machine.
MAC_PERSISTENT_CACHE_PATH = (
    '~/Library/Application Support/Spotify/PersistentCache/Storage')
LINUX_PERSISTENT_CACHE_PATH = os.path.join(
    os.getenv('XDG_CACHE_HOME', '~/.cache'), 'spotify/Storage')

PERSISTENT_CACHE_PATH = (
    MAC_PERSISTENT_CACHE_PATH if sys.platform == 'darwin'
    else LINUX_PERSISTENT_CACHE_PATH)


def parse(file_name, user_id):
    """
    Parse a Spotify PersistentStorage file with folder structure at start.

    `file_name`
        Location of a PersistentStorage file.
    `user_id`
        Specify a user id to use for folder URIs. Can also be a
        placeholder value like 'unknown'. (Background: this information
        doesn't seem to be provided in the source file.)
    """
    with open(file_name, 'rb') as data_file:
        data = data_file.read()

    # spotify:playlist, spotify:start-group, spotify:end-group
    rows = re.split(b'spotify:[pse]', data)
    folder = {'type': 'folder', 'children': []}
    stack = []

    for row in rows:
        # Note: '\r' marks end of repeated block. This might break in
        # future versions of Spotify. An alternative solution is to read
        # the number of repeats coded into the protobuf file.
        chunks = row.split(b'\r', 1)
        row = chunks[0]
        if row.startswith(b'laylist:'):
            folder['children'].append({
                'type': 'playlist',
                'uri': 'spotify:p' + row[:-1].decode('utf-8')
            })
        elif row.startswith(b'tart-group:'):
            stack.append(folder)
            tags = row.split(b':')
            folder = dict(
                # Assuming folder names < 128 characters.
                # Alternatively, do a protobuf varint parser to get length.
                name=unquote_plus(tags[-1][:-1].decode('utf-8')),
                type='folder',
                uri=(
                    'spotify:user:%s:folder:' % user_id
                    + tags[-2].decode('utf-8')
                ),
                children=[]
            )
        elif row.startswith(b'nd-group:'):
            parent = stack.pop()
            parent['children'].append(folder)
            folder = parent
        if folder.get('children') and len(chunks) > 1:
            break

    # close any remaining groups -- sometimes cache files contain errors.
    while len(stack) > 0:
        parent = stack.pop()
        parent['children'].append(folder)
        folder = parent

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


def get_all_persistent_cache_files(path):
    """Get all files in PersistentCache storage with "start-group" marker."""
    path = os.path.expanduser(path)
    shell_command = (
        'grep -rls "start-group" "{path}" --null | xargs -0 ls -t'
    ).format(path=path)
    output = subprocess.check_output(shell_command, shell=True).strip()
    if output:
        return output.split(b'\n')
    else:
        return []


def print_info_text(number):
    """Prints info text for `number` of PersistentCache storage files."""
    suffix = 'y' if number == 1 else 'ies'
    message = 'Found {number} folder hierarch{suffix} on this machine.'.format(
        number=number, suffix=suffix)
    if number > 1:
        message += (
            '\n\n'
            'To see the second one, run'
            '\n\n'
            '  spotifyfolders --account 2\n')
    print(message)


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
        '-i', '--info', action='store_const', const=True, default=False,
        help='Information about Spotify folders on this machine.')
    parser.add_argument(
        '-a', '--account', dest='account', default='1',
        help=('Sometimes a machine has multiple Spotify accounts. This gets a '
              'Spotify folder hierachy of a specific account. 1 is the most '
              'recently updated account, 2 is the second most recently '
              'updated account, etc.'))
    parser.add_argument(
        '--cache', dest='cache_dir', default=PERSISTENT_CACHE_PATH,
        help='Specify a custom PersistentCache directory to look for data in.')
    parser.add_argument(
        '-h', '--help', action='help', default=argparse.SUPPRESS,
        help='Show this help message and exit.')

    args = parser.parse_args()
    cache_files = get_all_persistent_cache_files(args.cache_dir)

    if args.info:
        print_info_text(len(cache_files))
    else:
        if not args.account.isdigit() or int(args.account) == 0:
            print('Specify account as a positive number. See `--help`.')
            exit(0)

        cache_file_index = int(args.account) - 1
        if cache_file_index >= len(cache_files):
            print('No data found in Spotify cache. If you have a custom cache '
                  'directory set, specify its path with the `--cache` flag.')
            exit(0)

        cache_file_name = cache_files[cache_file_index]
        print(_process(cache_file_name, args))
