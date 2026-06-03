import io
import json
import os
import py_compile
import re
import sqlite3
import subprocess
import threading
import time

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

load_dotenv()
APP_ID, APP_SECRET = os.environ["APP_ID"], os.environ["APP_SECRET"]
HOME = os.path.expanduser("~")

# 飞书 API 客户端：用于主动调用接口（发消息、查信息等）
client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

procs = {}  # 卡片消息id -> 正在跑的 claude 子进程，用于强制停止
stopped = set()  # 被用户停止的卡片消息id
pending = {}  # chat_id -> (目录, sid)：待开新会话/续会话，下一条消息消费

# ---- SQLite：msgs=每次任务一行；sessions=每会话一行(cwd/title)；recent=最近目录 ----
DB = sqlite3.connect("bot.db", check_same_thread=False)
DB.executescript(
    "CREATE TABLE IF NOT EXISTS msgs("
    "card TEXT PRIMARY KEY, rid TEXT, chat TEXT, prompt TEXT, sid TEXT, "
    "cost TEXT, status TEXT, created INTEGER, ended INTEGER, result TEXT);"
    "CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, cwd TEXT, title TEXT, created INTEGER);"
    "CREATE TABLE IF NOT EXISTS recent(dir TEXT PRIMARY KEY, ts INTEGER);"
    "CREATE TABLE IF NOT EXISTS seen(mid TEXT PRIMARY KEY, ts INTEGER);"
)
DB.commit()
DBLOCK = threading.Lock()

# 查询时把会话表的 cwd/title 一起 JOIN 出来
SEL = ("SELECT m.card,m.rid,m.chat,m.prompt,m.sid,m.cost,m.status,m.created,m.ended,m.result,"
       "s.cwd,s.title FROM msgs m LEFT JOIN sessions s ON m.sid=s.sid ")
DKEYS = ["card", "rid", "chat", "prompt", "sid", "cost", "status", "created", "ended", "result", "cwd", "title"]


def dbw(sql, *p):
    with DBLOCK:
        DB.execute(sql, p)
        DB.commit()


def dbq(sql, *p):
    with DBLOCK:
        return DB.execute(sql, p).fetchall()


def seen_once(mid):
    """首次见到该消息 -> 记录并返回 True；已见过 -> 返回 False。持久化去重，重启后仍生效"""
    now = int(time.time())
    with DBLOCK:
        cur = DB.execute("INSERT OR IGNORE INTO seen(mid,ts) VALUES(?,?)", (mid, now))
        DB.execute("DELETE FROM seen WHERE ts < ?", (now - 86400,))  # 清理一天前的，控表大小
        DB.commit()
        return cur.rowcount > 0


def find(mid):
    """按引用的消息 id 定位一行：可能是 AI 答复 rid，也可能是停止后残留的卡片 card"""
    if not mid:
        return None
    r = dbq(SEL + "WHERE m.rid=? OR m.card=? ORDER BY m.created DESC LIMIT 1", mid, mid)
    return dict(zip(DKEYS, r[0])) if r else None


def latest(chat):
    r = dbq(SEL + "WHERE m.chat=? ORDER BY m.created DESC LIMIT 1", chat)
    return dict(zip(DKEYS, r[0])) if r else None


def ref_entry(msg, chat):
    """有引用用引用的会话，否则用该会话最近一次（即上一次 AI 返回的消息）"""
    return find(msg.parent_id) or latest(chat)


def get_recent():
    return [r[0] for r in dbq("SELECT dir FROM recent ORDER BY ts DESC")]


def get_sessions():
    """所有会话，按最近活动时间倒序，返回 (sid, cwd, title) 列表"""
    return dbq("SELECT s.sid, s.cwd, s.title FROM sessions s JOIN msgs m ON m.sid=s.sid "
               "GROUP BY s.sid ORDER BY MAX(m.created) DESC")


def reply(message_id: str, text: str):
    """把文本作为回复发回飞书对应的消息，返回新消息 id（用于绑定会话）"""
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}))
            .msg_type("text")
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    return resp.data.message_id if resp.success() else None


def btn(text, value, type="default"):
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": type, "value": value}


def card_obj(status: str, running: bool = False) -> dict:
    """构造卡片对象；running=True 时附带「强制停止」按钮"""
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": status}}]
    if running:
        elements.append({"tag": "action", "actions": [btn("🛑 强制停止", {"action": "stop"}, "danger")]})
    return {"elements": elements}


def recent_card_obj(dirs, page=0):
    """最近目录卡片：点目录=新建会话；带翻页和「磁盘浏览」切换按钮"""
    per = 8
    chunk = dirs[page * per:(page + 1) * per]
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "🕘 **最近目录**（点击新建会话）"}}]
    for d in chunk:
        elements.append({"tag": "action", "actions": [btn(f"📁 {os.path.basename(d) or d}",
                        {"action": "cd_pick", "path": d})]})
    nav = []
    if page > 0:
        nav.append(btn("◀️", {"action": "recent_page", "page": str(page - 1)}))
    if (page + 1) * per < len(dirs):
        nav.append(btn("▶️", {"action": "recent_page", "page": str(page + 1)}))
    nav.append(btn("💽 磁盘浏览", {"action": "to_dir"}))
    elements.append({"tag": "action", "actions": nav})
    return {"elements": elements}


def sessions_card_obj(rows, page=0):
    """会话列表卡片：每个会话一个按钮（标题·目录名），点击登记为待续会话；附翻页"""
    per = 8
    chunk = rows[page * per:(page + 1) * per]
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "💬 **会话列表**（点击继续）"}}]
    for sid, cwd, title in chunk:
        label = f"{title or '(无标题)'} · {os.path.basename(cwd) or cwd}"
        elements.append({"tag": "action", "actions": [btn(f"💬 {label}", {"action": "sess_pick", "sid": sid})]})
    nav = []
    if page > 0:
        nav.append(btn("◀️", {"action": "sess_page", "page": str(page - 1)}))
    if (page + 1) * per < len(rows):
        nav.append(btn("▶️", {"action": "sess_page", "page": str(page + 1)}))
    if nav:
        elements.append({"tag": "action", "actions": nav})
    return {"elements": elements}


def list_dirs(path):
    """path 为空 -> 盘符列表；否则 -> 子目录列表"""
    if not path:
        return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
    try:
        return sorted(os.path.join(path, d) for d in os.listdir(path)
                      if os.path.isdir(os.path.join(path, d)))
    except OSError:
        return []


def dir_card_obj(path, page=0):
    """文件系统浏览卡片：选盘 / 进入子目录 / 上一级 / 在此新建会话"""
    subs = list_dirs(path)
    per = 8
    chunk = subs[page * per:(page + 1) * per]
    title = "💽 **选择磁盘**" if not path else f"📂 **{path}**"
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": title}}]
    for d in chunk:
        label = d if not path else os.path.basename(d)
        elements.append({"tag": "action", "actions": [btn(f"📁 {label}",
                        {"action": "cd_enter", "path": d})]})
    nav = []
    if path:
        parent = os.path.dirname(path)
        up = "" if parent == path else parent  # 盘根再上一级 -> 回到磁盘列表
        nav.append(btn("⬆️ 上一级", {"action": "cd_enter", "path": up}))
    if page > 0:
        nav.append(btn("◀️", {"action": "cd_enter", "path": path, "page": str(page - 1)}))
    if (page + 1) * per < len(subs):
        nav.append(btn("▶️", {"action": "cd_enter", "path": path, "page": str(page + 1)}))
    if path:
        nav.append(btn("✅ 在此新建会话", {"action": "cd_pick", "path": path}, "primary"))
    nav.append(btn("🕘 最近目录", {"action": "to_recent"}))
    elements.append({"tag": "action", "actions": nav})
    return {"elements": elements}


def send_card(message_id: str, obj, msg_type="interactive"):
    """回复一张卡片，返回卡片消息 id（用于后续更新/绑定）"""
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps(obj))
            .msg_type(msg_type)
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    return resp.data.message_id if resp.success() else None


def delete_card(message_id: str) -> None:
    """删除已发出的状态卡片（任务完成后不再需要）"""
    client.im.v1.message.delete(DeleteMessageRequest.builder().message_id(message_id).build())


def reply_file(message_id: str, name: str, content: bytes):
    """上传一个文件并作为回复发回飞书，返回新消息 id（用于绑定会话）"""
    resp = client.im.v1.file.create(CreateFileRequest.builder().request_body(
        CreateFileRequestBody.builder().file_type("stream").file_name(name)
        .file(io.BytesIO(content)).build()).build())
    if not resp.success():
        return None
    r = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(
        ReplyMessageRequestBody.builder().content(json.dumps({"file_key": resp.data.file_key}))
        .msg_type("file").build()).build())
    return r.data.message_id if r.success() else None


def reply_md(message_id: str, md: str, title: str):
    """AI 结果以 markdown 卡片回复；内容过长（超卡片上限）则改发 .md 文件"""
    card = {"elements": [{"tag": "markdown", "content": md}]}
    if len(json.dumps(card).encode("utf-8")) <= 28000:
        rid = send_card(message_id, card)
        if rid:
            return rid
    name = re.sub(r'[\\/:*?"<>|\n\r]+', "_", title or "result")[:40] + ".md"
    return reply_file(message_id, name, md.encode("utf-8"))


def fmt_cost(data: dict) -> str:
    u = data.get("usage") or {}
    return (f"💰 ${data.get('total_cost_usd', 0):.4f} | "
            f"⏱ {data.get('duration_ms', 0) / 1000:.1f}s | "
            f"in {u.get('input_tokens', 0)} / out {u.get('output_tokens', 0)} tok")


def fmt_status(e: dict) -> str:
    label = {"running": "⏳ 执行中", "done": "✅ 已完成",
             "stopped": "🛑 已停止", "error": "❌ 出错"}.get(e["status"], e["status"])
    if e["ended"] and e["created"]:
        label += f" | ⏱ {(e['ended'] - e['created']) / 1000:.1f}s"
    if e.get("title"):
        label += f"\n📝 {e['title']}"
    return label


def ask_claude(prompt: str, cwd: str, sid, card_id) -> dict:
    """调用 Claude Code；sid 非空则 --resume 续会话。进程存入 procs[card_id] 以便强停"""
    cmd = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"]
    if sid:
        cmd += ["--resume", sid]
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", shell=True,  # Windows 上 claude 是 .cmd，需要 shell
    )
    procs[card_id] = proc
    out, err = proc.communicate(input=prompt)  # prompt 经 stdin 传入，避免含换行被 cmd.exe 截断
    procs.pop(card_id, None)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"result": (err or "调用失败").strip()}


HELP = (
    "🤖 命令：\n"
    "/new <prompt> — 用上次目录新建会话并执行（无则~）\n"
    "/cd <路径> — 在该目录新建会话\n"
    "/cd — 卡片选最近目录/磁盘浏览后新建会话\n"
    "/sessions — 卡片选历史会话继续\n"
    "/md — 把结果导出为 markdown 文件（引用逻辑同下）\n"
    "/pwd — 查会话目录（有引用用引用，否则上一次）\n"
    "/cost — 查花费（同上）\n"
    "/status — 查状态（同上）\n"
    "/restart — 重启服务（先自检语法）\n"
    "/help — 帮助\n\n"
    "直接发消息=续上一次会话；引用某AI回复=续该会话"
)


def handle_cmd(text, msg, chat):
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    mid = msg.message_id
    if cmd == "/new":
        if not arg:
            reply(mid, "用法：/new <prompt>")
        else:
            e = latest(chat)  # 自动用上次 AI 的目录，无则 ~
            run_task(arg, msg, chat, new_dir=(e["cwd"] if e and e["cwd"] else HOME))
    elif cmd == "/cd":
        if not arg:
            send_card(mid, recent_card_obj(get_recent()))  # 先最近目录，卡片内可切磁盘浏览
        elif not os.path.isdir(arg):
            reply(mid, f"目录不存在: {arg}")
        else:
            pending[chat] = (arg, None)
            reply(mid, f"📂 将在 {arg} 新建会话，请发送指令")
    elif cmd == "/sessions":
        rows = get_sessions()
        send_card(mid, sessions_card_obj(rows)) if rows else reply(mid, "暂无会话")
    elif cmd == "/md":
        e = ref_entry(msg, chat)
        if e and e["result"]:
            name = re.sub(r'[\\/:*?"<>|\n\r]+', "_", e["title"] or "result")[:40] + ".md"
            reply_file(mid, name, e["result"].encode("utf-8"))
        else:
            reply(mid, "未找到结果（任务未完成或无结果）")
    elif cmd == "/pwd":
        e = ref_entry(msg, chat)
        reply(mid, f"📁 {e['cwd']}" if e else "未找到会话")
    elif cmd == "/cost":
        e = ref_entry(msg, chat)
        reply(mid, fmt_cost(json.loads(e["cost"])) if e and e["cost"] else "无花费数据（任务未完成？）")
    elif cmd == "/status":
        e = ref_entry(msg, chat)
        reply(mid, fmt_status(e) if e else "未找到会话")
    elif cmd == "/restart":
        try:  # 先自检语法，挡住把自己改崩导致 SCM 崩溃重启循环
            py_compile.compile(__file__, doraise=True)
        except py_compile.PyCompileError as e:
            reply(mid, f"❌ 语法不通过，不重启：\n{e}")
        else:
            reply(mid, "🔄 正在重启…约 10 秒后回来")  # 必须在自杀前发出去
            # 延迟自杀：先让本函数返回，lark 才会给飞书回 ACK，否则飞书会重发 /restart 致循环重启
            threading.Timer(2, lambda: os._exit(1)).start()  # 非零退出 → SCM 按 onfailure 拉起
    elif cmd == "/help":
        reply(mid, HELP)
    else:
        reply(mid, "未知命令，/help 查看")


# 卡片按钮回调：强制停止 / 目录浏览 / 选定目录。
# 必须把新卡片作为返回值回传，飞书才会更新；用 patch 会被飞书回滚。
def on_card_action(data) -> P2CardActionTriggerResponse:
    v = data.event.action.value or {}
    a = v.get("action")
    chat = data.event.context.open_chat_id
    if a == "cd_enter":
        return P2CardActionTriggerResponse(
            {"card": {"type": "raw", "data": dir_card_obj(v["path"], int(v.get("page", 0)))}})
    if a == "to_dir":
        return P2CardActionTriggerResponse({"card": {"type": "raw", "data": dir_card_obj("")}})
    if a in ("recent_page", "to_recent"):
        return P2CardActionTriggerResponse(
            {"card": {"type": "raw", "data": recent_card_obj(get_recent(), int(v.get("page", 0)))}})
    if a == "cd_pick":
        pending[chat] = (v["path"], None)
        return P2CardActionTriggerResponse({
            "toast": {"type": "success", "content": "已选择，请发送指令"},
            "card": {"type": "raw", "data": {"elements": [{"tag": "div", "text": {
                "tag": "lark_md", "content": f"📂 已选择 **{v['path']}**\n请发送指令开新会话"}}]}}})
    if a == "sess_page":
        return P2CardActionTriggerResponse(
            {"card": {"type": "raw", "data": sessions_card_obj(get_sessions(), int(v["page"]))}})
    if a == "sess_pick":
        row = dbq("SELECT cwd, title FROM sessions WHERE sid=?", v["sid"])
        if not row:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "会话不存在"}})
        cwd, title = row[0]
        pending[chat] = (cwd, v["sid"])  # 登记为待续会话
        return P2CardActionTriggerResponse({
            "toast": {"type": "success", "content": "已选会话，请发送指令"},
            "card": {"type": "raw", "data": {"elements": [{"tag": "div", "text": {"tag": "lark_md",
                "content": f"💬 已选会话 **{title or v['sid'][:8]}**\n📂 {cwd}\n请发送指令继续"}}]}}})
    # stop：杀掉对应 claude 进程
    card_id = data.event.context.open_message_id
    proc = procs.get(card_id)
    if proc:
        stopped.add(card_id)  # 先标记，再杀：避免 work 线程抢先把"失败"当结果发出
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        status = "🛑 已停止"
    else:
        status = "（任务已结束）"
    return P2CardActionTriggerResponse({"card": {"type": "raw", "data": card_obj(status)}})


def run_task(text, msg, chat, new_dir=None):
    """子线程里调用 Claude；new_dir 非空则强制在该目录开新会话（/new 用）"""
    def work():
        card_id = send_card(msg.message_id, card_obj("✅ 已触发，正在执行…", running=True))
        if new_dir is not None:
            cwd, sid = new_dir, None
        else:
            pend = pending.pop(chat, None)
            if pend is not None:
                cwd, sid = pend  # 命令登记的 (目录, 会话)；sid 为 None 则开新会话
            else:
                e = ref_entry(msg, chat)
                cwd, sid = (e["cwd"], e["sid"]) if e else (HOME, None)  # 续会话，无则在 ~ 新建
        dbw("INSERT OR REPLACE INTO msgs(card,chat,prompt,sid,status,created) "
            "VALUES(?,?,?,?,'running',?)", card_id, chat, text, sid, int(time.time() * 1000))
        data = ask_claude(text, cwd, sid, card_id)
        end = int(time.time() * 1000)
        if card_id in stopped:  # 被强制停止：保留「已停止」卡片，仅记录状态
            stopped.discard(card_id)
            dbw("UPDATE msgs SET status='stopped', ended=? WHERE card=?", end, card_id)
            return
        answer = data.get("result") or "（无输出）"
        rid = reply_md(msg.message_id, answer, text)
        new_sid = data.get("session_id") or sid
        cost = json.dumps({"total_cost_usd": data.get("total_cost_usd", 0),
                           "duration_ms": data.get("duration_ms", 0),
                           "usage": data.get("usage") or {}})
        dbw("UPDATE msgs SET rid=?, sid=?, cost=?, status='done', ended=?, result=? WHERE card=?",
            rid, new_sid, cost, end, answer, card_id)
        # 会话表：新会话首次落库，title 取首条 prompt（resume 时已存在则保留）
        if new_sid:
            dbw("INSERT OR IGNORE INTO sessions(sid,cwd,title,created) VALUES(?,?,?,?)",
                new_sid, cwd, text[:50], end)
        if sid is None:  # 新建会话：记录目录到最近列表（去重）
            dbw("INSERT OR REPLACE INTO recent VALUES(?,?)", cwd, end // 1000)
        if card_id:
            delete_card(card_id)  # 完成后删掉状态卡片

    threading.Thread(target=work, daemon=True).start()


# 接收消息事件：用户私聊或群里 @ 机器人时触发
def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    if not seen_once(msg.message_id):  # 去重：跨重启持久，避免重启后重发的消息被再次执行
        return
    chat = msg.chat_id
    text = json.loads(msg.content).get("text", "")
    for m in (msg.mentions or []):  # 去掉群聊 @ 占位符
        text = text.replace(m.key, "")
    text = text.strip()

    if text.startswith("/"):  # 命令不发给 AI
        handle_cmd(text, msg, chat)
    else:
        run_task(text, msg, chat)  # claude 调用较慢，放子线程


handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(on_message)
    .register_p2_card_action_trigger(on_card_action)  # 卡片按钮回调
    .build()
)

def notify_start():
    """服务启动后，向最近一次对话推送一张「已启动」卡片提醒"""
    rows = dbq("SELECT chat FROM msgs WHERE chat IS NOT NULL ORDER BY created DESC LIMIT 1")
    if not rows:
        return
    client.im.v1.message.create(CreateMessageRequest.builder().receive_id_type("chat_id")
        .request_body(CreateMessageRequestBody.builder().receive_id(rows[0][0])
            .msg_type("interactive").content(json.dumps(card_obj("🟢 feishu-cc 服务已启动"))).build()).build())


# 长连接客户端：通过 WebSocket 接收飞书推送的事件（无需公网地址）
if __name__ == "__main__":
    notify_start()
    lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler).start()
