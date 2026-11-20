"""Offline upload regressions; all provider responses are mocked."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from baidu_downloader import api, cli, model


def provider(body, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    return result


class UploadPlans(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root/"drama").mkdir()
        (self.root/"drama"/"a.mp4").write_bytes(b"x"*(5*1024*1024+7))
        (self.root/"drama"/"sub").mkdir()
        (self.root/"drama"/"sub"/"b.jpg").write_bytes(b"y")

    def test_plan_enumerates_and_validates_round_trip(self):
        plan = model.build_upload_plan([self.root/"drama"], "/备份/短剧", None)
        model.validate_upload_plan(json.loads(json.dumps(plan)))
        self.assertEqual(plan["directory"], "/备份/短剧")
        self.assertEqual([row["remote_path"] for row in plan["files"]], ["/备份/短剧/a.mp4", "/备份/短剧/sub/b.jpg"])
        self.assertEqual(plan["total_bytes"], 5*1024*1024+7+1)

    def test_plan_tampering_is_rejected(self):
        plan = model.build_upload_plan([self.root/"drama"], "/备份", None)
        plan["files"][0]["remote_path"] = "/elsewhere/a.mp4"
        with self.assertRaises(model.InputError):
            model.validate_upload_plan(plan)

    def test_symlink_and_traversal_sources_are_rejected(self):
        (self.root/"link.mp4").symlink_to(self.root/"drama"/"a.mp4")
        with self.assertRaises(model.InputError):
            model.build_upload_plan([self.root/"link.mp4"], "/x", None)
        with self.assertRaises(model.InputError):
            model.build_upload_plan([self.root], "/a/../b", None)

    def test_digest_blocks_match_reference_semantics(self):
        size, content, blocks, slice_md5 = api.digest_blocks(self.root/"drama"/"a.mp4")
        import hashlib
        raw = (self.root/"drama"/"a.mp4").read_bytes()
        self.assertEqual(size, len(raw))
        self.assertEqual(content, hashlib.md5(raw).hexdigest())
        self.assertEqual(slice_md5, hashlib.md5(raw[:api.SLICE_MD5_BYTES]).hexdigest())
        self.assertEqual(blocks[0], hashlib.md5(raw[:api.SLICE]).hexdigest())
        self.assertEqual(blocks[1], hashlib.md5(raw[api.SLICE:]).hexdigest())
        self.assertEqual(len(blocks), 2)


class UploadProtocol(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.file = self.root/"f.bin"
        self.payload = bytes(range(256))*(api.SLICE//256+11)
        self.file.write_bytes(self.payload)
        self.digests = api.digest_blocks(self.file)
        self.client = Mock()

    def test_quota_parses_and_validates(self):
        self.client.get.return_value = provider({"errno": 0, "total": 1000, "used": 400})
        self.assertEqual(api.quota(self.client), (1000, 400))
        self.client.get.return_value = provider({"errno": 0, "total": 100, "used": 200})
        with self.assertRaises(api.ProviderError):
            api.quota(self.client)

    def test_precreate_normalizes_and_rejects_bad_shapes(self):
        self.client.post.return_value = provider({"errno": 0, "return_type": 1, "uploadid": "UP1", "block_list": [0, 1]})
        result = api.precreate(self.client, "/x/f.bin", self.digests, 123)
        self.assertEqual(result["uploadid"], "UP1")
        self.assertEqual(result["pending"], [0, 1])
        form = self.client.post.call_args.kwargs["data"]
        self.assertEqual(json.loads(form["block_list"]), self.digests[2])
        self.assertEqual(form["rtype"], "2")
        self.client.post.return_value = provider({"errno": 0, "return_type": 2, "uploadid": "", "block_list": []})
        self.assertTrue(api.precreate(self.client, "/x/f.bin", self.digests, 123)["rapid"])
        self.client.post.return_value = provider({"errno": 0, "return_type": 1, "uploadid": "U", "block_list": [99]})
        with self.assertRaises(api.ProviderError):
            api.precreate(self.client, "/x/f.bin", self.digests, 123)

    def test_upload_slice_streams_and_verifies_digest(self):
        sent = {}

        def consuming_post(url, params=None, data=None, headers=None):
            sent["bytes"] = b"".join(data)  # real transport would stream this body
            return provider({"errno": 0, "md5": "ignored"})

        self.client.post.side_effect = consuming_post
        api.upload_slice(self.client, "UP", str(self.file), "/x/f.bin", 0, 2, self.digests)
        url = self.client.post.call_args.args[0]
        params = self.client.post.call_args.kwargs["params"]
        self.assertIn("superfile2", url)
        self.assertEqual(params["partseq"], "0")
        self.assertEqual(params["uploadid"], "UP")
        self.assertEqual(sent["bytes"].count(bytes(range(256))), api.SLICE//256)
        # Corrupted local slice must be caught by the streaming digest check.
        digests = list(self.digests)
        digests[2] = ["0"*32]+self.digests[2][1:]
        with self.assertRaises(model.InputError):
            api.upload_slice(self.client, "UP", str(self.file), "/x/f.bin", 0, 2, digests)

    def test_finish_create_returns_identity(self):
        self.client.post.return_value = provider({"errno": 0, "fs_id": 4242, "path": "/x/f.bin"})
        result = api.finish_create(self.client, "/x/f.bin", self.digests, "UP", 123)
        self.assertEqual(result["fs_id"], 4242)
        self.assertEqual(json.loads(self.client.post.call_args.kwargs["data"]["block_list"]), self.digests[2])
        self.client.post.return_value = provider({"errno": 0})
        with self.assertRaises(api.ProviderError):
            api.finish_create(self.client, "/x/f.bin", self.digests, "UP", 123)


class UploadWorkflow(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.job = self.root/"upjob"
        self.media = self.root/"media"
        self.media.mkdir()
        (self.media/"small.mp4").write_bytes(b"z"*1024)

    def test_upload_plan_command_reports_quota(self):
        client = Mock()
        client.get.return_value = provider({"errno": 0, "total": 10**9, "used": 3*10**8})
        with patch.object(cli, "credentials", return_value=("offline", "offline")), \
             patch.object(cli.api, "session", return_value=client):
            code = cli.main(["upload-plan", "--paths", str(self.media), "--remote-dir", "/备份", "--job", str(self.job)])
        self.assertEqual(code, 0)
        plan = model.read(self.job/"plan.json")
        model.validate_upload_plan(plan)
        review = (self.job/"plan-review.md").read_text()
        self.assertIn("SUFFICIENT", review)
        self.assertIn(f"UPLOAD {plan['plan_id']}", review)

    def test_upload_refuses_when_quota_insufficient(self):
        plan = model.build_upload_plan([self.media], "/备份", None)
        model.write(self.job/"plan.json", plan)
        model.write(self.job/"approval.json", {"plan_id": plan["plan_id"], "kind": "upload"})
        client = Mock()
        client.get.return_value = provider({"errno": 0, "total": 10**6, "used": 999999})
        emitted = []
        with patch.object(cli, "credentials", return_value=("offline", "offline")), \
             patch.object(cli.api, "session", return_value=client), \
             patch.object(cli, "emit", side_effect=lambda event, **values: emitted.append((event, values))):
            code = cli.main(["upload", "--job", str(self.job)])
        self.assertEqual(code, 1)
        self.assertEqual(emitted[-1][0], "stopped")
        self.assertIn("Insufficient quota", emitted[-1][1]["message"])
        # mkdir was never reached: no upload POST happened.
        self.assertEqual(client.post.call_count, 0)

    def test_upload_runs_full_chain_and_writes_receipts(self):
        plan = model.build_upload_plan([self.media], "/备份", None)
        model.write(self.job/"plan.json", plan)
        model.write(self.job/"approval.json", {"plan_id": plan["plan_id"], "kind": "upload"})
        client = Mock()
        client.get.return_value = provider({"errno": 0, "total": 10**9, "used": 0})

        def consuming_post(url, params=None, data=None, headers=None):
            if isinstance(data, (list, tuple)) or hasattr(data, "__next__"):
                b"".join(data)
            return provider({"errno": 0, "return_type": 1, "uploadid": "UP", "block_list": [0], "fs_id": 77, "path": "/备份/small.mp4", "md5": "x"})

        client.post.side_effect = consuming_post
        with patch.object(cli, "credentials", return_value=("offline", "offline")), \
             patch.object(cli.api, "session", return_value=client):
            code = cli.main(["upload", "--job", str(self.job), "--workers", "2"])
        self.assertEqual(code, 0)
        receipt_dir = self.job/"upload-receipts"/plan["plan_id"]
        receipts = list(receipt_dir.glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = model.read(receipts[0])
        self.assertEqual(receipt["state"], "complete")
        self.assertEqual(receipt["fs_id"], 77)
        self.assertFalse(receipt["rapid"])
        status = model.read(self.job/"status.json")
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["completed_files"], 1)
        # Second run skips the completed file without any provider calls.
        calls = client.post.call_count
        with patch.object(cli, "credentials", return_value=("offline", "offline")), \
             patch.object(cli.api, "session", return_value=client):
            code = cli.main(["upload", "--job", str(self.job)])
        self.assertEqual(code, 0)
        self.assertEqual(client.post.call_count, calls)


if __name__ == "__main__":
    unittest.main()
