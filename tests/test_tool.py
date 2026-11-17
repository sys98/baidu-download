"""Offline behavioral regressions; no account or provider requests are used."""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import requests
from PIL import Image

from baidu_downloader import api, cli, model, transfer


def source():
    return {"id": "1", "title": "Example drama", "share_url": "https://pan.baidu.com/s/1example", "expected_episodes": 2}


def entry(identifier, path, size=3, kind="video"):
    return {"fs_id": identifier, "path": path, "size": size, "kind": kind}


def response(body, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {"Content-Length": str(len(body))})
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [body]
    return result


class Contracts(unittest.TestCase):
    def test_session_guards_all_send_paths_without_global_monkeypatch(self):
        original = requests.Session.send
        client = api.session("offline", "offline")
        with patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("Network forbidden")):
            for url in ("http://pan.baidu.com/", "https://pan.baidu.com.evil.invalid/", "https://user@pan.baidu.com/", "https://pan.baidu.com:444/"):
                with self.assertRaises(ValueError):
                    client.get(url)
            with self.assertRaises(ValueError):
                client.get("https://pan.baidu.com/", verify=False)
        self.assertIs(requests.Session.send, original)

    def test_provider_error_body_and_html_are_never_exposed(self):
        result = Mock(status_code=404)
        with self.assertRaises(api.ProviderError) as caught:
            api.checked(result, "share_list")
        self.assertNotIn("https", str(caught.exception))
        result = Mock(status_code=200)
        result.json.side_effect = ValueError("private response")
        with self.assertRaises(api.ProviderError) as caught:
            api.checked(result, "share_list")
        self.assertNotIn("private", str(caught.exception))

    def test_repaired_status_decoder_and_request_encoding(self):
        result = Mock(status_code=200)
        result.json.return_value = {"no": 0}
        self.assertEqual(api.checked(result, "account_sync"), {"no": 0})
        self.assertEqual(api.rc4(b"Plaintext", b"Key").hex(), "bbf316e8d940af0ad3")
        client = api.session("offline-placeholder", "offline")
        reply = Mock(status_code=200)
        reply.json.return_value = {"errno": 0, "list": [{"dlink": "http://d.pcs.baidu.com/file"}]}
        constants = dict(deviceID="fixture", appVersion="fixture", appSignatureCertMD5="fixture", hmacDynamicKey="fixture")
        with patch.object(client, "post", return_value=reply) as post:
            url = api.get_link({"session": client, "protocol": constants, "uid": "123", "key": b"fixture"},
                               {"uk": "1", "shareid": "2", "sekey": "a+b/c="}, {"fs_id": 987})
        form = parse_qs(post.call_args.kwargs["data"])
        self.assertEqual(json.loads(form["extra"][0])["sekey"], "a+b/c=")
        self.assertEqual(json.loads(form["fid_list"][0]), ["987"])
        self.assertTrue(url.startswith("https://"))

    def test_root_leading_one_and_nested_pagination(self):
        client = Mock()
        def reply(items, more=False):
            result = Mock(status_code=200)
            result.json.return_value = {"errno": 0, "data": {"list": items, "more": more}}
            return result
        def item(identifier, path, directory=0):
            return {"fs_id": str(identifier), "path": path, "size": "0" if directory else "3", "isdir": str(directory)}
        client.post.side_effect = [reply([item(1,"/a",1)]), reply([item(2,"/a/x.mp4")],True), reply([item(3,"/a/y.mp4")])]
        result = api.scan_share(client, source()["share_url"], "0000")
        self.assertEqual(len(result), 3)
        self.assertEqual(client.post.call_args_list[0].kwargs["data"]["shorturl"], "1example")
        self.assertEqual(client.post.call_args_list[2].kwargs["data"]["page"], "2")
        client.post.side_effect = [reply([item(2,"/a/x.mp4")],True)]*2
        with self.assertRaises(ValueError):
            api.scan_share(client, source()["share_url"], "0000")

    def test_transient_connection_retry_is_bounded(self):
        operation = Mock(side_effect=[requests.exceptions.SSLError(), requests.Timeout(), "ok"])
        with patch.object(api.time, "sleep"):
            self.assertEqual(api.retry(operation), "ok")
        operation = Mock(side_effect=requests.ConnectionError())
        with patch.object(api.time, "sleep"), self.assertRaises(requests.ConnectionError):
            api.retry(operation)
        self.assertEqual(operation.call_count, 3)


class Planning(unittest.TestCase):
    def test_opaque_groups_singletons_and_explicit_cover_variants(self):
        entries = [entry(1,"/opaque/a.mp4"),entry(2,"/opaque/b.mp4"),entry(3,"/other/c.mp4"),
                   entry(4,"/other/d.mp4"),entry(5,"/single/e.mp4"),entry(6,"/opaque/one.png",kind="image"),entry(7,"/opaque/two.png",kind="image")]
        groups = model.groups(entries)
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(not group["content_verified"] for group in groups))
        data = {"sources":[source()],"inventories":{"1":entries}}
        with tempfile.TemporaryDirectory() as temp:
            plan = model.build_plan(data,[{"source_id":"1","file_ids":[1],"cover_ids":[6,7]}],temp)
            self.assertEqual(len(plan["files"]),3)
            self.assertEqual(plan["warnings"][0]["selected_videos"],1)
            model.validate_plan(plan)
            plan["destination"] = str(Path(temp)/"changed")
            with self.assertRaises(ValueError): model.validate_plan(plan)

    def test_source_normalization_separates_codes_and_rejects_collisions(self):
        normalized,codes=model.sources([{**source(),"share_url":source()["share_url"]+"?pwd=0000"}])
        self.assertEqual(codes,{"1":"0000"})
        self.assertNotIn("pwd", json.dumps(normalized))
        for title in ("../escape","bad/name","bad\\name",""):
            with self.assertRaises(ValueError): model.sources([{**source(),"title":title}])
        with self.assertRaises(ValueError): model.sources([source(),{**source(),"id":2,"title":"EXAMPLE DRAMA"}])

    def test_empty_or_unscanned_selection_does_not_create_plan(self):
        data={"sources":[source()],"inventories":{"1":[entry(1,"/x/a.mp4")]}}
        for selection in ([],[{"source_id":"1"}],[{"source_id":"1","file_ids":[999]}]):
            with self.assertRaises(ValueError): model.build_plan(data,selection,".")


class Transfers(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Optional real local media fixture requires ffmpeg/ffprobe")
    def test_real_local_mp4_is_verified_without_filename_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"fixture.mp4"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=c=black:s=32x48:d=0.2","-c:v","libx264","-pix_fmt","yuv420p",str(path)],check=True,capture_output=True,timeout=30)
            partial=path.with_suffix(".part");path.rename(partial)
            media=transfer.probe(partial,"video")
            self.assertTrue(any(item["codec_type"]=="video" for item in media["media"]["streams"]))

    def test_owned_partial_resumes_only_missing_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);job=base/"job"
            data={"sources":[source()],"inventories":{"1":[entry(1,"/x/a.mp4")]}}
            plan=model.build_plan(data,[{"source_id":"1","file_ids":[1]}],base/"media")
            file=plan["files"][0];target,partial,marker=transfer.paths(job,plan,file)
            target.parent.mkdir(parents=True);partial.write_bytes(b"a")
            with self.assertRaises(ValueError): transfer.existing(job,plan,file)
            model.write(marker,{"identity":transfer.identity(plan,file),"state":"partial"})
            client=Mock();client.get.return_value=response(b"bc",206,{"Content-Length":"2","Content-Range":"bytes 1-2/3"})
            progress=Mock()
            with patch.object(api,"get_link",return_value="https://d.pcs.baidu.com/file"), patch.object(transfer,"probe",return_value={"kind":"video","media":{}}):
                result=transfer.download(job,plan,file,{"session":client},{},progress)
            self.assertEqual(target.read_bytes(),b"abc")
            self.assertEqual(result["transferred_bytes"],2)
            self.assertFalse(partial.exists())
            progress.assert_called_once_with(2,3)

    def test_exact_range_and_byte_mismatch_never_append_wrong_data(self):
        with tempfile.TemporaryDirectory() as temp:
            partial=Path(temp)/"file.part";partial.write_bytes(b"a")
            client=Mock();client.get.return_value=response(b"bc",206,{"Content-Length":"2","Content-Range":"bytes 1-2/3"})
            progress=Mock()
            transfer.receive(client,"https://d.pcs.baidu.com/file",partial,3,progress)
            self.assertEqual(partial.read_bytes(),b"abc")
            self.assertEqual(client.get.call_args.kwargs["headers"]["Range"],"bytes=1-")
            partial.write_bytes(b"a")
            client.get.return_value=response(b"abc",200)
            with self.assertRaises(ValueError): transfer.receive(client,"https://d.pcs.baidu.com/file",partial,3,progress)
            self.assertEqual(partial.read_bytes(),b"a")
            partial.unlink();client.get.return_value=response(b"abc",200)
            with self.assertRaises(ValueError): transfer.receive(client,"https://d.pcs.baidu.com/file",partial,2,progress)
            self.assertFalse(partial.exists())

    def test_invalid_images_and_video_streams_fail_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"file.part";path.write_bytes(b"bad")
            with self.assertRaises(OSError): transfer.probe(path,"image")
            with patch.object(transfer.subprocess,"run",return_value=Mock(returncode=0,stdout='{"streams":[]}')):
                with self.assertRaises(ValueError): transfer.probe(path,"video")

    def test_interrupted_publication_can_recover_verified_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);job=base/"job"
            buffer=io.BytesIO();Image.new("RGB",(8,12),"red").save(buffer,format="PNG")
            data={"sources":[source()],"inventories":{"1":[entry(1,"/x/a.png",len(buffer.getvalue()),kind="image")]}}
            plan=model.build_plan(data,[{"source_id":"1","cover_ids":[1]}],base/"media")
            file=plan["files"][0];target,partial,marker=transfer.paths(job,plan,file)
            target.parent.mkdir(parents=True)
            partial.write_bytes(buffer.getvalue())
            receipt={"identity":transfer.identity(plan,file),"state":"verified_partial","bytes":file["size"],"mtime_ns":partial.stat().st_mtime_ns,"probe":transfer.probe(partial,"image")}
            model.write(marker,receipt);os.link(partial,target)
            self.assertEqual(transfer.existing(job,plan,file)["state"],"complete")
            self.assertFalse(partial.exists())


class Workflow(unittest.TestCase):
    def test_stale_live_inventory_cannot_request_links(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);job=base/"job"
            original=entry(1,"/x/a.mp4")
            plan=model.build_plan({"sources":[source()],"inventories":{"1":[original]}},[{"source_id":"1","file_ids":[1]}],base/"media")
            model.write(job/"plan.json",plan)
            with redirect_stdout(io.StringIO()), patch.object(cli,"credentials",return_value=("offline","offline")), \
                 patch.object(cli,"extraction_codes",return_value={"1":"0000"}), patch.object(api,"session"), \
                 patch.object(api,"scan_share",return_value=[{**original,"size":99}]), patch.object(api,"get_link") as link:
                self.assertEqual(cli.main(["download","--job",str(job),"--confirm","DOWNLOAD "+plan["plan_id"]]),1)
                link.assert_not_called()
            self.assertFalse((job/"approval.json").exists())

    def test_provider_failure_stops_queued_work(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);job=base/"job"
            entries=[entry(1,"/x/a.mp4"),entry(2,"/x/b.mp4")]
            plan=model.build_plan({"sources":[source()],"inventories":{"1":entries}},[{"source_id":"1","groups":[1]}],base/"media")
            model.write(job/"approval.json",{"plan_id":plan["plan_id"]})
            with redirect_stdout(io.StringIO()), patch.object(api,"session"), patch.object(api,"account",side_effect=api.ProviderError("account_sync",code=-6)) as account:
                self.assertEqual(cli.run_download(job,plan,{"1":"0000"},("offline","offline"),{},1),1)
                self.assertEqual(account.call_count,1)
            state=model.read(job/"status.json")
            self.assertEqual(state["state"],"requires_attention")
            self.assertEqual(state["failed_files"],1)

    def test_offline_scan_plan_confirmation_download_resume_status_and_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);job=base/"job";image=base/"fixture.png"
            Image.new("RGB",(8,12),"red").save(image)
            blobs={"/a/opaque.mp4":b"sample-video", "/a/cover.png":image.read_bytes()}
            entries=[entry(1,"/a/opaque.mp4",len(blobs['/a/opaque.mp4'])),entry(2,"/a/cover.png",len(blobs['/a/cover.png']),"image")]
            client=Mock()
            client.get.side_effect=lambda url,**kwargs:response(blobs[url.split(".com",1)[1]])
            fake_probe=lambda path,kind: {"kind":"video","media":{"streams":[{"codec_type":"video"}]}} if kind=="video" else real_probe(path,kind)
            real_probe=transfer.probe
            with redirect_stdout(io.StringIO()), patch.object(cli,"credentials",return_value=("offline","offline")), \
                 patch.dict(os.environ,{"BAIDU_SHARE_CODE":"0000"}), patch.object(api,"session",return_value=client), \
                 patch.object(api,"scan_share",return_value=entries), patch.object(api,"protocol",return_value={}), \
                 patch.object(api,"account",return_value={"session":client}), patch.object(api,"open_share",return_value={}), \
                 patch.object(api,"get_link",side_effect=lambda ctx,share,item:"https://d.pcs.baidu.com"+item["path"]) as link, \
                 patch.object(transfer,"probe",side_effect=fake_probe):
                self.assertEqual(cli.main(["scan","--url",source()["share_url"],"--title","Example drama","--job",str(job)]),0)
                self.assertNotIn("0000",(job/"scan.json").read_text())
                model.write(job/"selection.json",[{"source_id":"1","groups":[1],"cover_ids":[2]}])
                self.assertEqual(cli.main(["plan","--job",str(job),"--destination",str(base/"media")]),0)
                self.assertEqual(cli.main(["download","--job",str(job),"--confirm","wrong"]),1)
                link.assert_not_called()
                plan=model.read(job/"plan.json")
                self.assertEqual(cli.main(["download","--job",str(job),"--confirm","DOWNLOAD "+plan["plan_id"]]),0)
                self.assertEqual(link.call_count,2)
                self.assertEqual(cli.main(["download","--job",str(job)]),0)
                self.assertEqual(link.call_count,2)
                status=model.read(job/"status.json")
                self.assertEqual(status["completed_files"],2)
                self.assertEqual(status["new_bytes"],0)
                self.assertEqual(cli.main(["status","--job",str(job)]),0)
                self.assertEqual(cli.main(["verify","--job",str(job),"--deep"]),0)
                target=base/"media"/"Example drama"/"opaque.mp4"
                target.write_bytes(b"changed")
                self.assertEqual(cli.main(["verify","--job",str(job)]),1)
                with self.assertRaises(FileExistsError): transfer.existing(job,plan,plan["files"][0])


if __name__ == "__main__":
    unittest.main()
