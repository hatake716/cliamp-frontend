"""タグを持つ実ファイルでローカルアルバムの範囲と並びを確認する。"""

import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cliamp_music import album
from cliamp_music.local_album import mutagen
from cliamp_music.protocol import Track, encode_request


@unittest.skipIf(mutagen is None, "ローカルの音声タグを読む mutagen がありません")
class LocalAlbumTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.library = Path(tmp.name)
        self.folder = self.library / "Album"

    def song(self, filename, number=1, *, disc=None, artist="歌手", albumartist="歌手",
             release="2026", album_name="作品集", folder=None):
        path = (folder or self.folder) / filename
        path.parent.mkdir(exist_ok=True, parents=True)
        # FLAC STREAMINFO with one second of 44.1 kHz stereo for the real tag reader.
        audio_info = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
        streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6) + audio_info.to_bytes(8, "big") + bytes(16)
        path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)
        audio = mutagen.File(path, easy=True)
        audio.update({"tracknumber": f"{number}/10", "artist": artist, "title": f"曲 {number}"})
        for tag, value in (("album", album_name), ("albumartist", albumartist), ("date", release),
                           ("discnumber", str(disc) if disc else None)):
            if value:
                audio[tag] = value
        audio.save()
        return path

    def test_multidisc_tags_order_and_release_boundary(self):
        disc_one = self.folder / "Disc 1"
        disc_two = self.folder / "Disc 02"
        first = self.song("zzz.flac", 1, folder=disc_one, artist="ゲスト")
        second = self.song("aaa.flac", 2, folder=disc_one)
        third = self.song("ccc.flac", 1, folder=disc_two)
        self.song("other.flac", 3, folder=disc_two, albumartist="同名の別歌手")
        self.song("different-year.flac", 4, folder=disc_two, release="2001")
        self.song("unrelated.flac", 5, folder=disc_two, album_name="別作品")
        self.song("other-edition.flac", 6, folder=self.library / "Another Edition")
        self.song("bonus.flac", 7, folder=self.folder / "Other Album")
        (disc_one / "link.flac").symlink_to(first)
        (self.folder / "Disc 09").symlink_to(self.library / "Another Edition")
        (disc_one / "list.m3u").write_text(str(first))
        song = Track(path=str(second), title="曲 2")
        self.assertTrue(album.can_resolve_album(song))
        with patch.object(album.spotify_import, "fetch_text", side_effect=AssertionError("unexpected network")):
            result = album.resolve_album(song)
        self.assertEqual([track.local_path for track in result], [str(first), str(second), str(third)])
        self.assertEqual((result[0].artist, result[0].album), ("ゲスト", "作品集"))

    def test_tagged_song_with_no_album_and_ambiguous_number(self):
        current = self.song("one.flac", 1, album_name=None)
        with self.assertRaises(ValueError):
            album.resolve_album(Track(path=str(current)))
        self.song("one.flac", 1)
        self.song("duplicate.flac", 1)
        with self.assertRaises(ValueError):
            album.resolve_album(Track(path=str(current)))

    def test_explicit_disc_numbers_and_file_uri(self):
        second = self.song("late.flac", 1, disc=2)
        first = self.song("first.flac", 3, disc=1)
        current = Track(path=second.as_uri())
        resolved = album.resolve_album(current)
        self.assertEqual([t.local_path for t in resolved], [str(first), str(second)])
        for path in ("https://example.org/track.flac", "file://remote/tmp/track.flac", "/tmp/library.m3u"):
            with self.subTest(path=path):
                self.assertFalse(album.can_resolve_album(Track(path=path)))

    def test_non_utf8_sibling_survives_wire_encoding(self):
        current = self.song("one.flac", 1)
        other = self.song(os.fsdecode(b"two-\xff.flac"), 2)
        tracks = album.resolve_album(Track(path=str(current)))
        self.assertEqual(os.fsencode(tracks[1].local_path), os.fsencode(str(other)))
        encode_request("playlist_add", tracks=list(tracks))
