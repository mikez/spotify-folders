#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Get your Spotify folder hierarchy with playlists into JSON.

    :license: MIT, see LICENSE for more details.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import re
import sys


try:
    from urllib import unquote_plus  # Python 2
except ImportError:
    from urllib.parse import unquote_plus  # Python 3


LEVELDB_ROOTLIST_KEY = b"!pl#slc#\x1dspotify:user:{}:rootlist#"

if sys.platform == "darwin":
    # Mac
    PERSISTENT_CACHE_PATH = (
        "~/Library/Application Support/Spotify/PersistentCache/Users"
    )
elif sys.platform == "win32":
    # Windows, via Microsoft store or standalone
    windows_appdata_path = os.getenv("LOCALAPPDATA")
    windows_store_path = os.path.join(
        windows_appdata_path,
        "Packages\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0\\LocalState" "\\Spotify\\Users",
    )
    if os.path.exists(windows_store_path):
        PERSISTENT_CACHE_PATH = windows_store_path
    else:
        PERSISTENT_CACHE_PATH = os.path.join(windows_appdata_path, "Spotify\\Users")
else:
    # Linux
    PERSISTENT_CACHE_PATH = os.path.join(
        os.getenv("XDG_CACHE_HOME", "~/.cache"), "spotify/Users"
    )


def parse(data, user_id):
    """
    Parse a Spotify PersistentStorage file with folder structure at start.

    `data`
        Raw data of the rootlist value stored in the PersistentStorage LevelDB.
    `user_id`
        Specify a user id to use for folder URIs. Can also be a
        placeholder value like 'unknown'. (Background: this information
        doesn't seem to be provided in the source file.)

    FILE STRUCTURE
    --------------
    The file resembles a binary Protocol Buffers file with some twists.
    The structure as changed as of 2023-11-30 and needs to be reexamined.

    The old structure was as follows:

      1. `00` hexstring.
      2. Spotify version number. Encoded as varint.
         (E.g. `114800625` is version `1.1.48.625`.)
      3. `A40115` hexstring with unknown meaning.
      4. Number of {playlist, start-group, end-group} strings.
         Encoded as varint of `(number << 3) | 001`.
      5. List of any of these three playlist string types:
        - playlist identifier
          (e.g. "spotify:playlist:37i9dQZF1DXdCsscAsbRNz")
        - folder start identifier
          (e.g. "spotify:start-group:8212237ac7347bfe:Summer")
        - folder end identifier
          (e.g. "spotify:end-group:8212237ac7347bfe")
      6. Other content we currently ignore.
    """
    # spotify:playlist, spotify:start-group, spotify:end-group
    rows = re.split(rb"spotify:(?=[pse])", data)
    folder = {"type": "folder", "children": []}
    stack = []

    for index, row in enumerate(rows):
        # Note: '\x10' marks the end of the entire list. This might
        # break in future versions of Spotify. Here are two alternative
        # solutions one might consider then:
        #   1. Read the length encoded as a varint before each string.
        #   2. Read the number of repeats specified in the beginning of
        #      the file.
        chunks = row.split(b"\x12", 1)
        row = chunks[0]
        if row.startswith(b"playlist:"):
            folder["children"].append(
                {"type": "playlist", "uri": "spotify:" + row.decode("utf-8")}
            )
        elif row.startswith(b"start-group:"):
            stack.append(folder)
            tags = row.split(b":")
            folder = dict(
                # Assuming folder names < 128 characters.
                # Alternatively, do a protobuf varint parser to get length.
                name=unquote_plus(tags[-1].decode("utf-8")),
                type="folder",
                uri=(
                    "spotify:user:%s:folder:" % user_id
                    + tags[-2].decode("utf-8").zfill(16)
                ),
                children=[],
            )
        elif row.startswith(b"end-group:"):
            parent = stack.pop()
            parent["children"].append(folder)
            folder = parent

        # if folder.get("children") and len(chunks) > 1:
        #     break

    # close any remaining groups -- sometimes a file contains errors.
    while len(stack) > 0:
        parent = stack.pop()
        parent["children"].append(folder)
        folder = parent

    return folder


def get_folder(folder_id, data):
    """Get a specific folder in data output by `parse()`."""
    data_type = data.get("type")
    if data_type == "folder":
        if data.get("uri", "").endswith(folder_id):
            return data
        for child in data.get("children", []):
            folder = get_folder(folder_id, child)
            if folder:
                return folder


def get_leveldb_rootlist(username, cachedir):
    rootpath = os.path.expanduser(cachedir)
    if username:
        dirpath = os.path.join(rootpath, username) + "-user"
    else:
        dirpath = rootpath
    return SpotifyLevelDB.get(username, dirpath)


def get_usernames(users_directory_path):
    basepath = os.path.expanduser(users_directory_path)
    return [
        folder.rsplit("-")[0]
        for folder in os.listdir(basepath)
        if folder.endswith("-user")
    ]


def print_info_text(usernames):
    """Prints info text about Spotify users with PersistentCache storage files."""
    number = len(usernames)
    suffix = "" if number == 1 else "s"
    if number:
        print()
    print(f"Found {number} Spotify account{suffix} on this machine", end="")
    if number:
        print(":")
    else:
        print(".")
        return
    print()
    for name in usernames:
        print(" -", name)
    print()
    print(
        "To see the folder hierarchy of a specific user, run"
        "\n\n"
        "  spotifyfolders --account NAME\n"
    )


def _process(raw_data, args, user_id="unknown", folder_id=None):
    data = parse(raw_data, user_id=user_id)

    # postprocessing
    if folder_id:
        data = get_folder(folder_id, data)
        if not data:
            print("Folder not found :(")
            sys.exit(1)

    return json.dumps(data)


# ======================================================================
# Bare-bones LevelDB reader
# see also: https://github.com/google/leveldb/
#
# Background: It seems many struggle with installing `plyvel`, a
# LevelDB decoder for Python; hence, this bare-bones LevelDB decoder.
# ======================================================================


class SpotifyLevelDB:
    @staticmethod
    def get(username, db_dirpath):
        # passing None as a username will try to deduce a username
        # from the given files; the last modified user is returned first.
        key = SpotifyLevelDB.make_key_from_username(username) if username else None
        files_to_examine = get_files_in_dir_modified_last_first(db_dirpath)

        def seek(file_suffix, reader_cls, key):
            for filepath in files_to_examine:
                if not filepath.endswith(file_suffix):
                    continue
                if not key:
                    key = SpotifyLevelDB.make_key_from_filepath(filepath)
                    if not key:
                        continue
                value = reader_cls.find(key, filepath)
                if value:
                    username = SpotifyLevelDB.extract_username_from_filepath(filepath)
                    return username, value

        # Case 1: check log files (no external libraries needed)
        result = seek(".log", LogReader, key)
        if result:
            return result

        # Case 2: check ldb files (snappy may be needed)
        result = seek(".ldb", TableReader, key)
        if result:
            return result

        return None, None

    @staticmethod
    def extract_username_from_filepath(filepath):
        head = filepath
        last_head = ""
        while head != last_head:
            last_head = head
            head, tail = os.path.split(head)
            if tail.endswith("-user"):
                return tail.rsplit("-")[0]

    @staticmethod
    def make_key_from_filepath(filepath):
        username = SpotifyLevelDB.extract_username_from_filepath(filepath)
        if username:
            return SpotifyLevelDB.make_key_from_username(username)

    @staticmethod
    def make_key_from_username(username):
        return LEVELDB_ROOTLIST_KEY.replace(b"{}", username.encode())


# Descriptor files
# --------------------------------------------------
# When decoded, a descriptor file is a list of batches,
# each a list of (key, value)-pairs.
#
# Undecoded, a .log-file is a sequence of 32KB-blocks of
# batch-fragments (last one can be smaller in size).
# When these fragments are joined, they form a batch.

FULL_RECORD = 1
FIRST_RECORD = 2
MIDDLE_RECORD = 3
LAST_RECORD = 4


class LogReader:
    def __init__(self, file, size):
        self.reader = BytesReader(file, size)

    def __iter__(self):
        batch_buffer = io.BytesIO()
        for block in LogBlockSequence(self.reader):
            for fragment in block:
                batch_buffer.write(fragment.data)
                if fragment.type in (FULL_RECORD, LAST_RECORD):
                    yield Batch(batch_buffer)
                    clear(batch_buffer)

    @staticmethod
    def dump(filepath):
        with open(filepath, "rb") as file:
            for batch in LogReader(file, filesize(filepath)):
                print(f"--- batch {batch.sequence_number}")
                for command, args in batch:
                    print(" ", command, " ".join(map(repr, args)))

    @staticmethod
    def find(target_key, filepath):
        assert isinstance(target_key, bytes)
        last_value = None
        with open(filepath, "rb") as file:
            for batch in LogReader(file, filesize(filepath)):
                for command, args in batch:
                    if command != "put":
                        continue
                    key, value = args
                    if key == target_key:
                        last_value = value
        return last_value


class LogBlockSequence:
    def __init__(self, reader):
        self.reader = reader

    def __iter__(self):
        while True:
            if not self.reader.bytes_left():
                return
            yield LogBlock(self.reader)


class LogBlock:
    MAX_BLOCK_SIZE = 32 * 1024  # 32KB

    def __init__(self, reader):
        self.reader = reader.limit(LogBlock.MAX_BLOCK_SIZE)

    def __iter__(self):
        while True:
            if not self.reader.bytes_left():
                return
            yield Fragment(self.reader)


class Fragment:
    HEADER_SIZE = 7

    def __init__(self, reader):
        self.read_header(reader)
        self.data = reader.n_bytes(self.length)

    def read_header(self, reader):
        self.checksum = reader.uint(4)
        self.length = reader.uint(2)
        self.type = reader.uint(1)


class Batch:
    def __init__(self, buffer):
        self.reader = reader = Batch.make_reader(buffer)
        self.sequence_number = reader.uint(8)
        self.count = reader.uint(4)

    def __iter__(self):
        reader = self.reader
        if reader.bytes_left() == 0:
            return
        for i in range(self.count):
            value_type = reader.uint(1)
            size = reader.varint()
            key = reader.n_bytes(size)
            if value_type == 1:  # PUT
                size = reader.varint()
                value = reader.n_bytes(size)
                yield "put", (key, value)
            else:  # DEL
                yield "delete", (key,)
        assert reader.bytes_left() == 0

    @staticmethod
    def make_reader(buffer):
        buffer.seek(0)
        size = len(buffer.getbuffer())
        return BytesReader(buffer, size)


# Table files
# --------------------------------------------------
# When decoded, a table file contains five blocks:
# data, meta, metaindex, index, and footer.
#
# The data block contains sorted (key, value)-pairs
# split into several parts. Use the index to jump to
# the parts you're interested in.


class TableReader:
    def __init__(self, filepath):
        self.filepath = filepath
        self.initial_offset = 0

    def __iter__(self):
        while True:
            record = self.read_record()
            if not record:
                break
            yield record

    @staticmethod
    def dump(filepath):
        with open(filepath, "rb") as file:
            reader = BytesReader(file, filesize(filepath))
            footer = TableFooter(reader)
            for key, handle in TableIndex(footer.index_handle, reader):
                for key, value in TableData(handle, reader):
                    print(key.sequence_number, key.user_key, "=>", value)

    @staticmethod
    def find(target_key, filepath):
        assert isinstance(target_key, bytes)
        # return at first key found, since it seems repeated keys
        # are sorted by last-inserted first.
        with open(filepath, "rb") as file:
            reader = BytesReader(file, filesize(filepath))
            footer = TableFooter(reader)
            for internal_key, handle in TableIndex(footer.index_handle, reader):
                if not bytestring_less_or_equal(target_key, internal_key.user_key):
                    continue
                for internal_key, value in TableData(handle, reader):
                    if internal_key.user_key == target_key:
                        return value
                break


class TableBlock:
    def __init__(self, handle, reader):
        self.offset = handle.offset
        self.size = handle.size
        self.reader = reader
        self.read_compression(reader)
        # reader.n_bytes(4) # checksum
        self.read_data(reader)

    def read_compression(self, reader):
        reader.seek(self.offset + self.size)
        self.compression = reader.uint(1)

    def read_data(self, reader):
        reader.seek(self.offset)
        self.data = reader.n_bytes(self.size, compression=self.compression)

    def __iter__(self):
        yield from KeyValueReader(BytesReader.from_bytes(self.data))


class TableData(TableBlock):
    def __iter__(self):
        for key, value in super().__iter__():
            yield InternalKey.from_bytes(key), value


class TableIndex(TableBlock):
    def __iter__(self):
        for key, value in super().__iter__():
            yield InternalKey.from_bytes(key), BlockHandle.from_bytes(value)


class TableFooter:
    # https://github.com/google/leveldb/blob/main/table/format.h#L53
    MAGIC_NUMBER = 0xDB4775248B80FB57
    MAGIC_NUMBER_LENGTH = 8
    LENGTH = 4 * 10 + MAGIC_NUMBER_LENGTH

    def __init__(self, reader):
        self.check_magic_number(reader)
        self.read_handles(reader)

    def check_magic_number(self, reader):
        reader.seek(-TableFooter.MAGIC_NUMBER_LENGTH, os.SEEK_END)
        number = reader.uint(TableFooter.MAGIC_NUMBER_LENGTH)
        assert number == TableFooter.MAGIC_NUMBER

    def read_handles(self, reader):
        # handles are (offset, size)-pairs of index-blocks
        reader.seek(-TableFooter.LENGTH, os.SEEK_END)
        BlockHandle(reader)  # metaindex handle
        self.index_handle = BlockHandle(reader)


class KeyValueReader:
    # list of key/value-entries and a trailer of restarts
    # https://github.com/google/leveldb/blob/main/table/block_builder.cc#L16
    def __init__(self, reader):
        self.reader = reader

    def __iter__(self):
        reader = self.reader
        reader.seek(reader.size - 4)
        num_restarts = reader.uint(4)
        restart_offset = reader.size - (1 + num_restarts) * 4
        last_key = b""
        reader.seek(0)
        while reader.pos() < restart_offset:
            # https://github.com/google/leveldb/blob/main/table/block.cc#L48-L75
            shared = reader.varint()
            unshared = reader.varint()
            value_length = reader.varint()
            assert shared <= len(last_key)
            key_prefix = last_key[:shared]
            key_suffix = reader.n_bytes(unshared)
            key = last_key = key_prefix + key_suffix
            value = reader.n_bytes(value_length)
            yield key, value


class InternalKey:
    @staticmethod
    def from_bytes(bytestring):
        prefix, suffix = bytestring[:-8], bytestring[-8:]
        suffix_reader = BytesReader.from_bytes(suffix)
        result = InternalKey()
        result.user_key = prefix
        result.value_type = suffix_reader.uint(1)
        result.sequence_number = suffix_reader.uint(7)
        return result


class BlockHandle:
    def __init__(self, reader):
        self.offset = reader.varint()
        self.size = reader.varint()

    @staticmethod
    def from_bytes(bytestring):
        reader = BytesReader.from_bytes(bytestring)
        return BlockHandle(reader)


# Helpers


class BytesReader:
    def __init__(self, file, size):
        self.file = file
        self.size = size

    @staticmethod
    def from_bytes(bytestring):
        return BytesReader(io.BytesIO(bytestring), len(bytestring))

    # Readers

    def n_bytes(self, n_bytes, compression=None):
        raw_bytes = self.file.read(n_bytes)
        if not compression:
            return raw_bytes
        elif compression == 1:  # snappy
            try:
                import snappy
            except ImportError:
                print(
                    "Your information appears to be compressed with the snappy compression format.\n"
                    "\n"
                    "Install the snappy decompression library if possible:\n"
                    "\n"
                    "  /usr/bin/env python -m pip install python-snappy\n"
                )
                sys.exit(1)
            return snappy.uncompress(raw_bytes)
        else:
            raise NotImplementedError(f"compression type {compression} not known.")

    def n_varints(self, n):
        return [self.varint() for _ in range(n)]

    def uint(self, n_bytes):
        bytestring = self.n_bytes(n_bytes)
        return int.from_bytes(bytestring, byteorder="little")

    def varint(self):
        value = 0
        shift = 0
        while True:
            byte = self.n_bytes(1)[0]
            value |= (byte & 0b01111111) << shift
            if not (byte & 0b10000000):
                break
            shift += 7
        return value

    # Helpers

    def bytes_left(self):
        return max(0, self.size - self.pos())

    def limit(self, n_bytes):
        limited_file = LimitedFile(self.file, n_bytes)
        new_size = min(limited_file.max_offset, self.size)
        return BytesReader(limited_file, new_size)

    def pos(self):
        return self.file.tell()

    def seek(self, target, whence=os.SEEK_SET):
        return self.file.seek(target, whence)


class LimitedFile:
    def __init__(self, file, n_bytes):
        self.file = file
        self.max_offset = file.tell() + n_bytes

    def tell(self):
        return self.file.tell()

    def read(self, n_bytes=None):
        current_offset = self.tell()
        bytes_left = self.max_offset - current_offset
        assert bytes_left >= 0
        if n_bytes is None:
            n_bytes = bytes_left
        return self.file.read(min(n_bytes, bytes_left))


def bytestring_less_or_equal(bytestring1, bytestring2):
    """
    In LevelDB tables, you can specify a custom comparator.
    It seems Spotify's comparator behaves a bit like this;
    they call it "greenbase.KeyComparator".
    """
    # Compare byte by byte
    group_separator = 0x1D
    for byte1, byte2 in zip(bytestring1, bytestring2):
        if byte1 == group_separator and byte2 != group_separator:
            return False
        if byte1 != group_separator and byte2 == group_separator:
            return True
        if byte1 < byte2:
            return True
        elif byte1 > byte2:
            return False

    # If all bytes are equal, check the length
    return len(bytestring1) <= len(bytestring2)


def clear(buffer):
    buffer.truncate(0)
    buffer.seek(0)


def convert_escaped_string_to_bytes(s: str):
    r"""
    Turn '\\x1d\\x0f' into b'\x1d\x0f'.
    """
    # https://docs.python.org/3/library/codecs.html#standard-encodings
    return bytes(s.encode("utf-8").decode("unicode_escape"), "utf-8")


def filesize(filepath):
    return os.path.getsize(filepath)


def get_files_in_dir_modified_last_first(path):
    result = []
    for dirpath, dirnames, filenames in os.walk(path):
        result.extend(os.path.join(dirpath, filename) for filename in filenames)
    result.sort(key=modified, reverse=True)
    return result


def modified(filepath):
    try:
        return os.path.getmtime(filepath)
    except FileNotFoundError:
        return float("Inf")


# ======================================================================


# --------------------------------------------------
# Command line setup
# --------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Get your Spotify folder hierarchy with playlists into JSON."),
        add_help=False,
    )
    parser.add_argument(
        "folder",
        default=None,
        metavar="folder",
        nargs="?",
        help=(
            "Get only a specific Spotify folder. If omitted, returns entire "
            "hierarchy. A folder is specified by its URL or URI. "
            "Obtain this by dragging a folder into a Terminal window. "
            "Alternatively, click on a folder in Spotify and do Cmd+C."
        ),
    )
    parser.add_argument(
        "-i",
        "--info",
        action="store_const",
        const=True,
        default=False,
        help="Information about Spotify folders on this machine.",
    )
    parser.add_argument(
        "-a",
        "--account",
        dest="account",
        help=(
            "Sometimes a machine has multiple Spotify accounts. This gets a "
            "Spotify folder hierachy of a specific account. "
            "To see a list of all found accounts, use the `-i` flag."
        ),
    )
    parser.add_argument(
        "--cache",
        dest="cache_dir",
        default=PERSISTENT_CACHE_PATH,
        help="Specify a custom PersistentCache directory to look for data in.",
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        default=argparse.SUPPRESS,
        help="Show this help message and exit.",
    )

    args = parser.parse_args()

    usernames = get_usernames(args.cache_dir)
    if args.info:
        print_info_text(usernames)
        sys.exit(0)

    if args.account and args.account not in usernames:
        print(
            f"Unknown username {args.account!r}. To see all found usernames, use `--info`."
        )
        sys.exit(2)

    folder_id = None
    username = args.account
    if args.folder:
        uri = args.folder
        if "/" not in uri and ":" not in uri:
            print("Specify folder as a URL or Spotify URI. See `--help`.")
            sys.exit(2)
        separator = "/" if uri.find("/") > 0 else ":"
        username = uri.split(separator)[-3]
        folder_id = uri.split(separator)[-1]

    username, raw_rootlist = get_leveldb_rootlist(username, args.cache_dir)
    if not raw_rootlist:
        print(
            "No data found in the Spotify cache. If you have a custom cache\n"
            "directory set, specify its path with the `--cache` flag.\n"
            "Also, in the Spotify app, check "
            "Settings -> Offline storage location."
        )
        sys.exit(2)

    print(_process(raw_rootlist, username, folder_id))
