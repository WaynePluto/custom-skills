"""专用页只读协议回归；不依赖浏览器或上游安装。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/browser-use/scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]), \
     mock.patch.object(sys, "dont_write_bytecode", True):
    import chrome_session as session
    from chrome_host import ChromeError


class SessionTabTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"adapter": "chrome-entry-v1", "generation": "a" * 32,
                         "binding": "b" * 64, "pid": 123, "pong": True,
                         "session_tab_protocol": session.SESSION_TAB_PROTOCOL}
        self.request = {"meta": session.SESSION_TAB_META, "generation": "a" * 32,
                        "binding": "b" * 64}
        self.info = {"targetId": "dedicated", "type": "page", "url": "about:blank", "title": ""}
        self.cdp = SimpleNamespace(send_raw=mock.AsyncMock(return_value={"targetInfo": self.info}))
        self.connection = SimpleNamespace(dedicated_target_id="dedicated", target_id="user-tab", cdp=self.cdp)

    def reply(self, request=None):
        return asyncio.run(session.session_tab_reply(self.connection, request or self.request, self.identity))

    def assert_error(self, code, function, *args):
        with self.assertRaises(ChromeError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_only_reads_daemon_owned_id_not_current_or_other_blank_tabs(self):
        reply = self.reply()
        self.assertEqual(reply["tab"], {"targetId": "dedicated", "url": "about:blank", "title": ""})
        self.cdp.send_raw.assert_awaited_once_with("Target.getTargetInfo", {"targetId": "dedicated"})
        self.assertEqual(self.connection.target_id, "user-tab")

    def test_repeated_reads_do_not_create_or_navigate(self):
        self.assertEqual(self.reply(), self.reply())
        self.assertEqual(self.cdp.send_raw.await_args_list, [
            mock.call("Target.getTargetInfo", {"targetId": "dedicated"}),
            mock.call("Target.getTargetInfo", {"targetId": "dedicated"}),
        ])

    def test_navigated_owned_page_is_reported_without_overwrite(self):
        self.info.update(url="https://example.com/existing", title="Already used")
        self.assertEqual(self.reply()["tab"]["url"], self.info["url"])
        self.cdp.send_raw.assert_awaited_once_with("Target.getTargetInfo", {"targetId": "dedicated"})

    def test_wrong_generation_or_binding_is_rejected_before_cdp(self):
        for key in ("generation", "binding"):
            with self.subTest(key=key):
                self.assertIn("error", self.reply({**self.request, key: "stale"}))
        self.cdp.send_raw.assert_not_awaited()

    def test_missing_dedicated_id_never_falls_back_to_current_tab(self):
        for target in (None, "", 12, "x" * (session.MAX_TARGET_ID + 1)):
            with self.subTest(target=target):
                self.connection.dedicated_target_id = target
                self.assertIn("error", self.reply())
        self.cdp.send_raw.assert_not_awaited()

    def test_closed_target_or_timeout_returns_error_without_recovery(self):
        for error in (RuntimeError("Target closed"), asyncio.TimeoutError()):
            with self.subTest(error=error):
                self.cdp.send_raw.reset_mock()
                self.cdp.send_raw.side_effect = error
                response = self.reply()
                self.assertEqual(response["error"], "session_tab_unavailable")
                self.assertNotIn("Target closed", str(response))
                self.cdp.send_raw.assert_awaited_once_with("Target.getTargetInfo", {"targetId": "dedicated"})

    def test_replacement_during_query_and_wrong_target_type_are_rejected(self):
        async def replace(*args):
            self.connection.dedicated_target_id = "replacement"
            return {"targetInfo": self.info}
        self.cdp.send_raw.side_effect = replace
        self.assertIn("error", self.reply())
        self.connection.dedicated_target_id = "dedicated"
        self.cdp.send_raw.side_effect = None
        for update in ({"targetId": "user-tab"}, {"type": "browser"}):
            self.cdp.send_raw.return_value = {"targetInfo": {**self.info, **update}}
            self.assertIn("error", self.reply())

    def test_result_size_is_bounded_without_truncating_target_url(self):
        self.info["title"] = "x" * 2000
        self.assertEqual(len(self.reply()["tab"]["title"]), session.MAX_TITLE)
        self.info["url"] = "x" * (session.MAX_URL + 1)
        self.assertIn("error", self.reply())

    def test_invalid_target_info_fails_closed(self):
        for raw in ({}, {"targetInfo": None}, {"targetInfo": {**self.info, "url": None}},
                    {"targetInfo": {**self.info, "title": []}}):
            with self.subTest(raw=raw):
                self.cdp.send_raw.return_value = raw
                self.assertIn("error", self.reply())

    def test_client_requires_supported_protocol_before_private_request(self):
        request = mock.Mock()
        for protocol in (None, 0, 2):
            self.assert_error("session_tab_unsupported", session.read_session_tab,
                              {**self.identity, "session_tab_protocol": protocol}, request)
        request.assert_not_called()

    def test_client_validates_every_identity_field_before_returning_tab(self):
        for key in session.IDENTITY_KEYS:
            with self.subTest(key=key):
                response = {**self.reply(), key: "changed"}
                self.assert_error("unknown_daemon", session.read_session_tab,
                                  self.identity, mock.Mock(return_value=response))
        self.assert_error("unknown_daemon", session.read_session_tab,
                          self.identity, mock.Mock(return_value=None))

    def test_client_strips_unneeded_fields_and_rejects_failure_or_invalid_tab(self):
        reply = self.reply()
        reply["tab"]["extra"] = "private"
        request = mock.Mock(return_value=reply)
        result = session.read_session_tab(self.identity, request)
        self.assertEqual(set(result), {"targetId", "url", "title"})
        request.assert_called_once_with(self.request)
        for extra in ({"error": "session_tab_unavailable"}, {"tab": None},
                      {"tab": {"targetId": "arbitrary"}}):
            self.assert_error("session_tab_unavailable", session.read_session_tab,
                              self.identity, mock.Mock(return_value={**reply, **extra}))


if __name__ == "__main__":
    unittest.main()
