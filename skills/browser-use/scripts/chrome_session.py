"""只读核验本 session daemon 的专用页，不搜索、创建或切换标签。"""

import asyncio

from chrome_host import ChromeError

SESSION_TAB_META = "browser_use_session_tab"
SESSION_TAB_PROTOCOL = 1
IDENTITY_KEYS = ("adapter", "generation", "binding", "pid", "pong", "session_tab_protocol")
TARGET_INFO_TIMEOUT = 3
MAX_TARGET_ID = 256
MAX_URL = 8000
MAX_TITLE = 500


def valid_tab(tab):
    return (isinstance(tab, dict)
            and isinstance(tab.get("targetId"), str) and 0 < len(tab["targetId"]) <= MAX_TARGET_ID
            and isinstance(tab.get("url"), str) and len(tab["url"]) <= MAX_URL
            and isinstance(tab.get("title"), str) and len(tab["title"]) <= MAX_TITLE)


def read_session_tab(identity, request):
    """identity 必须来自已通过宿主进程/会话校验的 ping。"""
    if identity.get("session_tab_protocol") != SESSION_TAB_PROTOCOL:
        raise ChromeError("session_tab_unsupported", "当前 daemon 不支持专用页核验；请确认后对同一 session 执行 --stop 再 --ensure，不自动重连。")
    response = request({"meta": SESSION_TAB_META, "generation": identity["generation"],
                        "binding": identity["binding"]})
    if (not isinstance(response, dict)
            or any(response.get(key) != identity.get(key) for key in IDENTITY_KEYS)):
        raise ChromeError("unknown_daemon", "专用页响应身份已改变；本次未选择或新建标签。")
    if response.get("error"):
        raise ChromeError("session_tab_unavailable", "本 session 专用页已关闭、发生变化或无法核验；不会接管其他标签，请先检查现状。")
    tab = response.get("tab")
    if not valid_tab(tab):
        raise ChromeError("session_tab_unavailable", "专用页响应无效；不会按 URL 或列表位置猜测目标。")
    return {key: tab[key] for key in ("targetId", "url", "title")}


async def session_tab_reply(connection, request, identity):
    """仅由 daemon 在通过上游 token 校验后调用，读取其自身持有的专用 ID。"""
    failure = {**identity, "error": "session_tab_unavailable"}
    if (request.get("generation") != identity["generation"]
            or request.get("binding") != identity["binding"]):
        return failure
    target_id = connection.dedicated_target_id
    if (not isinstance(target_id, str) or not 0 < len(target_id) <= MAX_TARGET_ID
            or not connection.cdp):
        return failure
    try:
        response = await asyncio.wait_for(connection.cdp.send_raw(
            "Target.getTargetInfo", {"targetId": target_id}), TARGET_INFO_TIMEOUT)
        info = response["targetInfo"]
        if (connection.dedicated_target_id != target_id or not isinstance(info, dict)
                or info.get("type") != "page" or info.get("targetId") != target_id):
            return failure
        tab = {"targetId": target_id, "url": info.get("url", ""), "title": info.get("title", "")}
        if not isinstance(tab["title"], str):
            return failure
        tab["title"] = tab["title"][:MAX_TITLE]
        return {**identity, "tab": tab} if valid_tab(tab) else failure
    except Exception:
        # 目标消失或查询超时均不走上游 stale-session 自动恢复，避免创建替代空白页。
        return failure
