"""
HiAgent（企业智能体平台）AK/SK 直连 Seedream 文生图 / Seedance 视频 —— 单文件示例
依赖：pip install requests
命令行运行时保留同目录的 hiagent_network.py，以共用网关网络策略。

命令行：
  python hiagent_quickstart.py image "一只橘猫晒太阳" 16:9
  python hiagent_quickstart.py image "竖版海报" 9:16
  python hiagent_quickstart.py video "海边日落镜头推进" 16:9 720p 5 fast
  python hiagent_quickstart.py models
"""

import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import requests
import urllib3

if __package__:
    from .hiagent_network import configured_hiagent_base_url, use_hiagent_env_proxy
else:
    from hiagent_network import configured_hiagent_base_url, use_hiagent_env_proxy

urllib3.disable_warnings()

# ============ 1. 凭证与固定参数（生产环境建议改用环境变量，别写死/别提交Git）============
ACCESS_KEY_ID = os.getenv("HIAGENT_AK", "")
SECRET_KEY = os.getenv("HIAGENT_SK", "")
_GATEWAY = urlsplit(configured_hiagent_base_url())
HOST = _GATEWAY.hostname or ""  # 来自 HIAGENT_BASE_URL，例如 https://hiagent.example.com:30040
PORT = _GATEWAY.port or 443  # 平台 OpenAPI 网关端口
REGION = "cn-north-1"
VERSION = "2023-08-01"
ACCOUNT_ID = "1000000000"  # 单租户固定
SERVICE = "aigw"  # 签名服务名，固定 aigw
WORKSPACE_ID = os.getenv("HIAGENT_WORKSPACE_ID", "")

_client_configuration = ContextVar("hiagent_client_configuration", default=None)


@contextmanager
def client_configuration(access_key, secret_key, workspace_id, host, port):
    """Bind one request's credentials without changing another in-flight client."""
    token = _client_configuration.set((access_key, secret_key, workspace_id, host, port))
    try:
        yield
    finally:
        _client_configuration.reset(token)


def _credentials():
    return _client_configuration.get() or (ACCESS_KEY_ID, SECRET_KEY, WORKSPACE_ID, HOST, PORT)


# 模型服务ID（已在你空间授权，python hiagent_quickstart.py models 可查全量）
MODEL_IMAGE = "d9k56d4mgqo5afvrboqg"  # Doubao-Seedream-5.0-lite（文生图）
MODEL_VIDEO_FAST = "d9k55hkmgqo5afvrboo0"  # Seedance-2.0-fast：480p/720p，快
MODEL_VIDEO_PRO = "d9k53bcmgqo5afvrbok0"  # Seedance-2.0：最高1080p
# 文本对话模型（也可传其它已授权 text-generation 的 ID，python xxx.py models 可查）
MODEL_CHAT_LOW = "d9patpa9a42rehc7hb7g"  # chat-low（本地，免费，默认）
MODEL_CHAT_MAX = "d8oc29b2lklvd9bhs8h0"  # chat-max（本地，最强）
MODEL_SEED_PRO = "d9k545smgqo5afvrbon0"  # doubao-seed-2.0-pro（公网，豆包）


# ============ 2. 火山 V4 签名 + 统一请求（一般不用改）============
def _hmac(key, msg):
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _gateway_service_name(action):
    if action in {"ImageGen", "CreateImageEdit"}:
        return "图片服务"
    if action in {"CreateVideoGenTask", "GetVideoGenTask"}:
        return "视频服务"
    return "AI 服务"


def _parse_hiagent_gateway_response(response, service):
    """Parse HiAgent JSON even when Content-Type is missing or not application/json."""

    raw = response.content or b""
    if not raw.strip():
        raise RuntimeError(f"{service}返回了空错误（HTTP {response.status_code}），请稍后重试")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"{service}返回了无法解析的错误（HTTP {response.status_code}），请稍后重试"
        ) from exc
    if not isinstance(payload, dict):
        return payload
    err = (payload.get("ResponseMetadata") or {}).get("Error")
    if err:
        extra = err.get("Data") if isinstance(err.get("Data"), (dict, list, str)) else None
        detail = (
            err.get("Message")
            or err.get("Detail")
            or (json.dumps(extra, ensure_ascii=False)[:1500] if extra else None)
            or err.get("RequestId")
        )
        if not detail:
            detail = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:2000]
        raise RuntimeError("{}: {}".format(err.get("Code"), detail))
    return payload.get("Result", payload)


def _gateway_proxy_options():
    """Keep the tenant gateway direct unless proxy inheritance is explicitly enabled."""
    if use_hiagent_env_proxy():
        return {}
    # An empty dict still inherits requests' environment/system proxies. Supply
    # every applicable scheme explicitly, including ALL_PROXY's fallback.
    return {"proxies": {"http": "", "https": "", "all": ""}}


def call(action, body, timeout=300):
    """调用平台 Action，返回 Result。"""
    access_key, secret_key, workspace_id, gateway_host, gateway_port = _credentials()
    service = _gateway_service_name(action)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    now = datetime.datetime.now(datetime.UTC)
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    short = now.strftime("%Y%m%d")
    body_sha = hashlib.sha256(raw).hexdigest()
    host = "%s:%d" % (gateway_host, gateway_port)
    query = f"Action={action}&Region={REGION}&Version={VERSION}&X-Account-Id={ACCOUNT_ID}"
    heads = {
        "host": host,
        "content-type": "application/json",
        "x-content-sha256": body_sha,
        "x-date": xdate,
    }
    signed_headers = ";".join(sorted(heads))
    canonical_heads = "".join(f"{k}:{heads[k].strip()}\n" for k in sorted(heads))
    canonical_request = "\n".join(["POST", "/", query, canonical_heads, signed_headers, body_sha])
    scope = f"{short}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join(
        ["HMAC-SHA256", xdate, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signing_key = _hmac(_hmac(_hmac(_hmac(secret_key, short), REGION), SERVICE), "request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Date": xdate,
        "X-Content-Sha256": body_sha,
        "Authorization": f"HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "x-top-region": REGION,
        "WorkspaceID": workspace_id,
    }
    url = f"https://{host}/?{query}"
    last = None
    attempts = 1 if action == "ListModelByWorkspaceGrant" else 6
    for attempt in range(attempts):  # 内网域名偶发DNS抖动，重试
        try:
            r = requests.post(
                url,
                headers=headers,
                data=raw,
                verify=gateway_host != HOST,
                allow_redirects=False,
                timeout=timeout,
                **_gateway_proxy_options(),
            )
            return _parse_hiagent_gateway_response(r, service)
        except requests.exceptions.ConnectionError as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(2.5)
    raise RuntimeError(f"连接失败: {last}")


def _norm_query(params):
    from urllib.parse import quote

    return "&".join(
        "{}={}".format(quote(str(key), safe="-_.~"), quote(str(params[key]), safe="-_.~"))
        for key in sorted(params)
    )


def up_call(action, params=None, body=b"", content_type="application/json", timeout=60):
    """HiAgent UP 服务（UploadRaw / DownloadKey），签名 service=up、Version=2022-01-01。"""
    access_key, secret_key, workspace_id, gateway_host, gateway_port = _credentials()
    query_params = dict(params or {})
    query_params["Action"] = action
    query_params.setdefault("Version", "2022-01-01")
    raw = body if isinstance(body, (bytes, bytearray)) else b""
    now = datetime.datetime.now(datetime.UTC)
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    short = now.strftime("%Y%m%d")
    body_sha = hashlib.sha256(raw).hexdigest()
    host = "%s:%d" % (gateway_host, gateway_port)
    query = _norm_query(query_params)
    heads = {
        "content-type": content_type,
        "host": host,
        "x-content-sha256": body_sha,
        "x-date": xdate,
    }
    signed_headers = ";".join(sorted(heads))
    canonical_heads = "".join(f"{k}:{heads[k]}\n" for k in sorted(heads))
    canonical_request = "\n".join(["POST", "/", query, canonical_heads, signed_headers, body_sha])
    scope = "{}/{}/{}/request".format(short, REGION, "up")
    string_to_sign = "\n".join(
        ["HMAC-SHA256", xdate, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signing_key = _hmac(_hmac(_hmac(_hmac(secret_key, short), REGION), "up"), "request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": content_type,
        "X-Date": xdate,
        "X-Content-Sha256": body_sha,
        "Authorization": f"HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "x-top-region": REGION,
        "WorkspaceID": workspace_id,
    }
    url = f"https://{host}/?{query}"
    last = None
    for _ in range(6):
        try:
            r = requests.post(
                url,
                headers=headers,
                data=raw,
                verify=gateway_host != HOST,
                allow_redirects=False,
                timeout=timeout,
                **_gateway_proxy_options(),
            )
            return _parse_hiagent_gateway_response(r, "文件上传服务")
        except requests.exceptions.ConnectionError as e:
            last = e
            time.sleep(2.5)
    raise RuntimeError(f"连接失败: {last}")


def upload_raw(content, content_type="application/octet-stream", filename="file.bin", expire="15h"):
    """上传原始文件到 HiAgent UP，返回 Path/Size。平台多模态参考生视频走同一通道。"""
    if not content:
        raise RuntimeError("上传文件不能为空")
    result = up_call(
        "UploadRaw",
        {"Id": uuid.uuid4().hex, "Expire": expire, "Filename": filename or "file.bin"},
        content,
        content_type or "application/octet-stream",
        timeout=300,
    )
    if not isinstance(result, dict) or not result.get("Path"):
        raise RuntimeError("文件上传未返回存储路径")
    result["Url"] = public_file_url(result["Path"])
    return result


def public_file_url(path, key=None):
    """平台 CreateVideoGenTask 需要可被模型拉取的绝对 HTTPS 地址，相对路径会报 invalid url。"""
    from urllib.parse import quote

    if not path:
        raise RuntimeError("文件路径不能为空")
    if not key:
        downloaded = up_call("DownloadKey", {"Path": path}, b"", "application/json")
        key = (downloaded or {}).get("Key") if isinstance(downloaded, dict) else None
    if not key:
        raise RuntimeError("文件下载凭证为空")
    return "https://{}/api/proxy/down?Action=DownloadForPresign&Key={}&Path={}".format(
        _credentials()[3],
        quote(str(key), safe="=+-_"),
        quote(str(path), safe=""),
    )


# ============ 3. 文生图（同步，约20秒，宽高任意）============
MIN_IMAGE_PIXELS = 3686400  # Seedream 服务端要求：总像素至少 3,686,400（=2560×1440）


def generate_image(
    prompt, width=2560, height=1440, n=1, watermark=False, save_dir="generated_images"
):
    os.makedirs(save_dir, exist_ok=True)
    # 宽高需为32的倍数
    width, height = (width // 32) * 32 or 32, (height // 32) * 32 or 32
    # 总像素不足下限则按原比例等比放大（否则服务端报 image size must be at least 3686400 pixels）
    if width * height < MIN_IMAGE_PIXELS:
        ratio = width / height
        height = math.ceil(math.sqrt(MIN_IMAGE_PIXELS / ratio) / 32) * 32
        width = math.ceil(ratio * height / 32) * 32
        while width * height < MIN_IMAGE_PIXELS:
            height += 32
            width = math.ceil(ratio * height / 32) * 32
        print(
            "提示：目标像素过小，已按比例放大到 %dx%d（%d 像素）" % (width, height, width * height)
        )
    res = call(
        "ImageGen",
        {
            "Prompt": prompt,
            "ModelServiceID": MODEL_IMAGE,  # 图片用 ModelServiceID
            "Size": {"Width": width, "Height": height},  # 比例/画质就靠这里的像素
            "MaxImages": n,
            "Watermark": bool(watermark),
            "WebSearch": False,
            "WorkspaceID": WORKSPACE_ID,
        },
    )
    files = []
    for i, d in enumerate(res.get("Data") or []):
        data = requests.get(d["Url"], verify=False, timeout=300).content
        ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
        fn = os.path.join(save_dir, "img_%dx%d_%d.%s" % (width, height, i, ext))
        open(fn, "wb").write(data)
        files.append(fn)
    print("图片完成(%dx%d):" % (width, height), files)
    return files


# ============ 3.5 参考图/图片编辑（图生图，本地图或图片URL都行）============
def _to_file_info(ref):
    """把参考图转成接口要的 FileInfo：本地文件 -> base64 dataURL；http(s) 链接 -> 原样。"""
    if isinstance(ref, dict):
        return ref
    if os.path.exists(ref):
        with open(ref, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(ref)[1].lower().lstrip(".")
        mime = "jpeg" if ext in ("jpg", "jpeg", "") else ext
        return {
            "Url": f"data:image/{mime};base64,{b}",
            "Name": os.path.basename(ref),
            "ContentType": f"image/{mime}",
        }
    mime = "jpeg" if ref.lower().split("?")[0].endswith((".jpg", ".jpeg")) else "png"
    return {"Url": ref, "Name": "ref", "ContentType": f"image/{mime}"}


def _ensure_min_pixels(width, height):
    width, height = (width // 32) * 32 or 32, (height // 32) * 32 or 32
    if width * height < MIN_IMAGE_PIXELS:
        ratio = width / height
        height = math.ceil(math.sqrt(MIN_IMAGE_PIXELS / ratio) / 32) * 32
        width = math.ceil(ratio * height / 32) * 32
        while width * height < MIN_IMAGE_PIXELS:
            height += 32
            width = math.ceil(ratio * height / 32) * 32
        print(
            "提示：目标像素过小，已按比例放大到 %dx%d（%d 像素）" % (width, height, width * height)
        )
    return width, height


def edit_image(
    prompt,
    refs,
    width=1920,
    height=1920,
    n=1,
    negative=None,
    guidance_scale=None,
    seed=None,
    watermark=False,
    save_dir="generated_images",
):
    """图生图/参考图编辑。refs：单张或一个列表，每个可以是本地文件路径或图片URL。"""
    os.makedirs(save_dir, exist_ok=True)
    if isinstance(refs, str):
        refs = [refs]
    width, height = _ensure_min_pixels(width, height)
    body = {
        "WorkspaceID": WORKSPACE_ID,
        "ModelServiceID": MODEL_IMAGE,
        "Prompt": prompt,
        "ImageFiles": [_to_file_info(r) for r in refs],
        "Size": {"Width": width, "Height": height},
        "MaxImages": n,
        "Watermark": bool(watermark),
        "WebSearch": False,
    }
    if negative:
        body["NegativePrompt"] = negative  # 不想出现的内容
    if guidance_scale is not None:
        body["GuidanceScale"] = guidance_scale  # 参考图遵循度，不传自动
    if seed is not None:
        body["Seed"] = int(seed)
    res = call("CreateImageEdit", body, timeout=300)
    files = []
    for i, d in enumerate(res.get("Data") or []):
        data = requests.get(d["Url"], verify=False, timeout=300).content
        ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
        fn = os.path.join(save_dir, "edit_%dx%d_%d.%s" % (width, height, i, ext))
        open(fn, "wb").write(data)
        files.append(fn)
    print("参考图编辑完成(%dx%d):" % (width, height), files)
    return files


# ============ 4. 文生视频（异步：建任务→轮询→下载，约1-2分钟）============
def generate_video(
    prompt,
    ratio="16:9",
    resolution="720p",
    duration=5,
    model="fast",
    generate_audio=False,
    save_dir="generated_videos",
):
    """ratio: 16:9/9:16/1:1/4:3/3:4/21:9/adaptive；resolution: 480p/720p(/1080p仅pro)；duration: 4-15"""
    model_id = MODEL_VIDEO_PRO if model == "pro" else MODEL_VIDEO_FAST
    task = call(
        "CreateVideoGenTask",
        {
            "Prompt": prompt,
            "Model": model_id,  # 视频用 Model
            "Implement": "volcengine",  # 必填
            "Ratio": ratio,
            "Resolution": resolution,
            "Duration": int(duration),
            "GenerateAudio": bool(generate_audio),
            "Watermark": False,
            "WorkspaceID": WORKSPACE_ID,
        },
    )
    task_id = task["task_id"]
    print("视频任务已创建:", task_id, "排队生成中...")
    os.makedirs(save_dir, exist_ok=True)
    for _ in range(90):  # 最多等15分钟
        time.sleep(10)
        t = call("GetVideoGenTask", {"WorkspaceID": WORKSPACE_ID, "TaskID": task_id}, timeout=60)
        st = (t.get("Tasks") or [{}])[0].get("Status")
        print("  状态:", st)
        if st in ("succeeded", "success"):
            url = (t["Tasks"][0].get("Data") or [{}])[0].get("Url")
            fn = os.path.join(save_dir, f"{task_id}.mp4")
            open(fn, "wb").write(requests.get(url, verify=False, timeout=600).content)
            print("视频完成:", fn)
            return fn
        if st in ("failed", "error", "canceled"):
            raise RuntimeError(f"视频任务失败: {json.dumps(t, ensure_ascii=False)}")
    raise RuntimeError("视频生成超时")


def list_models():
    items, page = [], 1
    while True:
        res = call(
            "ListModelByWorkspaceGrant",
            {"WorkspaceID": WORKSPACE_ID, "PageNumber": page, "PageSize": 100},
            timeout=60,
        )
        its = res.get("Items") or []
        items += its
        if len(its) < 100:
            break
        page += 1
    for m in items:
        if m.get("Granted"):
            print("%s | %-15s | %s" % (m["ID"], m.get("Type"), m.get("Name")))


# ============ 4.5 文本对话（会话式 + 流式，自动建会话，可多轮）============
_MODEL_CACHE = {}


def _model_obj(model_id):
    if model_id not in _MODEL_CACHE:
        items, page = [], 1
        while True:
            res = call(
                "ListModelByWorkspaceGrant",
                {"WorkspaceID": WORKSPACE_ID, "PageNumber": page, "PageSize": 100},
                timeout=60,
            )
            its = res.get("Items") or []
            items += its
            if len(its) < 100:
                break
            page += 1
        m = next((x for x in items if x["ID"] == model_id), None)
        if not m:
            raise RuntimeError(f"模型未授权或不存在: {model_id}（运行 models 查看可用ID）")
        m = dict(m)
        m["ModelServiceID"] = m["ID"]
        _MODEL_CACHE[model_id] = m
    return _MODEL_CACHE[model_id]


def _post_stream(action, body, timeout=180):
    """与 call 相同的火山V4签名，但以 SSE 流式返回 response。"""
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    now = datetime.datetime.now(datetime.UTC)
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    short = now.strftime("%Y%m%d")
    body_sha = hashlib.sha256(raw).hexdigest()
    host = "%s:%d" % (HOST, PORT)
    query = f"Action={action}&Region={REGION}&Version={VERSION}&X-Account-Id={ACCOUNT_ID}"
    heads = {
        "host": host,
        "content-type": "application/json",
        "x-content-sha256": body_sha,
        "x-date": xdate,
    }
    signed_headers = ";".join(sorted(heads))
    canonical_heads = "".join(f"{k}:{heads[k].strip()}\n" for k in sorted(heads))
    canonical_request = "\n".join(["POST", "/", query, canonical_heads, signed_headers, body_sha])
    scope = f"{short}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join(
        ["HMAC-SHA256", xdate, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signing_key = _hmac(_hmac(_hmac(_hmac(SECRET_KEY, short), REGION), SERVICE), "request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Date": xdate,
        "X-Content-Sha256": body_sha,
        "Authorization": f"HMAC-SHA256 Credential={ACCESS_KEY_ID}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "x-top-region": REGION,
        "WorkspaceID": WORKSPACE_ID,
        "Accept": "text/event-stream",
    }
    url = f"https://{host}/?{query}"
    return requests.post(
        url,
        headers=headers,
        data=raw,
        verify=False,
        timeout=timeout,
        stream=True,
        **_gateway_proxy_options(),
    )


def create_chat(model_id=MODEL_CHAT_LOW):
    """新建一个对话会话，返回 (ConversationID, SessionID)。"""
    m = _model_obj(model_id)
    r = call(
        "CreateConversation",
        {"WorkspaceID": WORKSPACE_ID, "ConversationName": "api-chat", "Models": [m]},
        timeout=60,
    )
    ci = r["ConversationInfo"]
    return ci["ConversationID"], ci["ProjectList"][0]["SessionID"]


def chat(prompt, model_id=MODEL_CHAT_LOW, conv=None):
    """文本对话。conv=(ConversationID,SessionID)，不传则自动新建；返回 (回复文本, conv)。
    多轮：把上一次返回的 conv 再传进来即可。"""
    m = _model_obj(model_id)
    cid, sid = conv if conv else create_chat(model_id)
    wr = call(
        "BatchCreateMessages",
        {
            "WorkspaceID": WORKSPACE_ID,
            "ConversationID": cid,
            "MessageList": [
                {
                    "SessionID": sid,
                    "ConversationID": cid,
                    "MessageID": str(uuid.uuid4()),
                    "Role": "user",
                    "ContentType": "text",
                    "Content": prompt,
                }
            ],
        },
        timeout=60,
    )
    saved = (wr.get("MessageList") or [{}])[0]
    r = _post_stream(
        "Chat",
        {
            "WorkspaceID": WORKSPACE_ID,
            "ConversationID": cid,
            "SessionID": sid,
            "Models": [m],
            "MessageID": saved.get("MessageID"),
            "QueryID": saved.get("QueryID"),
            "Stream": True,
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"Chat 失败: {r.text[:300]}")
    parts = []
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
        except Exception:
            pass
    return "".join(parts), (cid, sid)


# ============ 5. 在你代码里就这么调 ============
if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "models":
        list_models()
    elif len(sys.argv) >= 3 and sys.argv[1] == "image":
        # 也可直接给像素：generate_image("提示词", width=1080, height=1920)
        ratios = {
            "1:1": (1, 1),
            "4:3": (4, 3),
            "3:4": (3, 4),
            "16:9": (16, 9),
            "9:16": (9, 16),
            "21:9": (21, 9),
        }
        rw, rh = ratios.get(sys.argv[3], (16, 9))
        long_edge = 2560
        w, h = (
            (long_edge, round(long_edge * rh / rw))
            if rw >= rh
            else (round(long_edge * rw / rh), long_edge)
        )
        generate_image(sys.argv[2], width=w, height=h)
    elif len(sys.argv) >= 3 and sys.argv[1] == "video":
        # video "提示词" 比例 分辨率 时长 模型
        ratio = sys.argv[3] if len(sys.argv) > 3 else "16:9"
        reso = sys.argv[4] if len(sys.argv) > 4 else "720p"
        dur = int(sys.argv[5]) if len(sys.argv) > 5 else 5
        md = sys.argv[6] if len(sys.argv) > 6 else "fast"
        generate_video(sys.argv[2], ratio=ratio, resolution=reso, duration=dur, model=md)
    elif len(sys.argv) >= 3 and sys.argv[1] == "chat":
        model_id = sys.argv[3] if len(sys.argv) > 3 else MODEL_CHAT_LOW
        answer, _ = chat(sys.argv[2], model_id=model_id)
        print(answer)
    elif len(sys.argv) >= 4 and sys.argv[1] == "edit":
        # edit "修改要求" 参考图路径或URL [宽] [高]
        w = int(sys.argv[4]) if len(sys.argv) > 4 else 1920
        hh = int(sys.argv[5]) if len(sys.argv) > 5 else 1920
        edit_image(sys.argv[2], sys.argv[3], w, hh)
    else:
        print(
            '用法:\n  python hiagent_quickstart.py chat "你好" [模型ID，默认chat-low]'
            '\n  python hiagent_quickstart.py image "提示词" 16:9'
            '\n  python hiagent_quickstart.py edit "改成水彩画" 参考图.jpg [宽] [高]'
            '\n  python hiagent_quickstart.py video "提示词" 16:9 720p 5 fast'
            "\n  python hiagent_quickstart.py models"
        )
