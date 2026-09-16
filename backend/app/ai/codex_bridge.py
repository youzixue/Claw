"""Managed ChatGPT sign-in through the official Codex app-server (stdio).

Only the CLI stores/refreshes credentials in Claw's private CODEX_HOME.
No auth.json or keychain contents are ever read by this module.

News is sent only after a verified named permission profile is selected by an
ephemeral thread: one empty readable directory, no inheritance/platform roots,
no tool network, and no approval escalation. Legacy sandbox is a lossy projection;
never use it alone or send the removed readOnly.access override.
Tool switches alone are NOT a security boundary; never relax the profile checks.
References: https://developers.openai.com/codex/app-server/ (permissions),
https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json,
and codex-rs/app-server/tests/suite/v2/permission_profile_list.rs.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlsplit
from urllib.request import getproxies
from uuid import UUID

from app.config.settings import settings


CHAT_BLOCKED = "Codex 未确认新闻专用隔离权限（仅空目录只读、工具无网络、无额外读取）；新闻尚未发送，请检查 CLI 权限协议兼容性。"
# These are real flags in the official config.schema.json (not an invented
# tool_choice switch). Current schemas use features.view_image, not tools.view_image.
_DISABLED_FEATURES = (
    "shell_tool", "view_image", "multi_agent", "multi_agent_mode", "multi_agent_v2",
    "collab", "enable_fanout", "enable_mcp_apps",
    "apps", "connectors", "plugins", "remote_plugin", "recommended_plugins",
    "unified_exec", "js_repl", "code_mode", "code_mode_host",
    "browser_use", "browser_use_external", "computer_use", "image_generation",
    "imagegenext", "in_app_browser", "in_app_local_automation", "hooks",
    "codex_hooks", "plugin_hooks", "memories", "memory_tool", "shell_snapshot",
    "web_search", "web_search_cached", "web_search_request", "tool_search",
    "tool_suggest", "skill_search", "skill_mcp_dependency_install",
)
_PROFILE = "claw_news_isolated"
_MAX_TEXT = 1024 * 1024
# Only single-agent text reasoning levels supported by this bridge. Unknown
# future levels must not silently enable a new execution mode (e.g. Ultra).
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_MESSAGES = {
    "unavailable": "未找到官方 Codex CLI；请管理员安装 @openai/codex 并配置 AI_CODEX_EXECUTABLE。",
    "disconnected": "尚未连接 ChatGPT；需要用户主动授权。",
    "pending": "等待用户完成官方 ChatGPT 授权。",
    "connected": "已通过官方 Codex CLI 连接 ChatGPT。",
    "completed": "授权已完成，等待账户状态确认。",
    "cancelled": "授权已取消。",
    "failed": "官方授权未完成或被拒绝，请重试。",
    "error": "Codex 服务不可用、协议异常或请求超时。",
}
_PLANS = {"free", "go", "plus", "pro", "team", "business", "enterprise", "edu", "unknown"}


class BridgeError(Exception):
    """Fixed safe error; never include stderr, RPC bodies, or credentials."""


def _login_id(value):
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def _auth_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in
                {"auth.openai.com", "auth0.openai.com", "chatgpt.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment or any(ord(ch) < 32 for ch in value)):
            return None
        # OAuth state/challenge are intended browser fields, bearer credentials are not.
        forbidden = {"access_token", "refresh_token", "id_token", "token", "api_key"}
        if any(key.lower() in forbidden for key, _ in parse_qsl(parsed.query)):
            return None
        return value
    except ValueError:
        return None


class CodexBridge:
    def __init__(self, config=None, *, timeout=20.0, turn_timeout=120.0):
        self._config = config if config is not None else settings
        self._timeout = timeout
        self._turn_timeout = turn_timeout
        self._chat_available = False
        self._chat_checked = False
        self._chat_busy = False
        self._login_completed_until = 0.0
        self._turns = {}
        self._process = None
        self._reader = None
        self._workspace = None
        self._pending = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._login_id = None
        self._login_status = "disconnected"
        self._connected = False
        self._plan = None
        self._completions = OrderedDict()

    def _executable(self):
        value = str(getattr(self._config, "AI_CODEX_EXECUTABLE", "codex")).strip()
        if not value:
            return None
        executable = shutil.which(value)
        if executable or value != "codex":
            return executable
        # macOS GUI/launchd PATH often omits CLI installs. Reuse only the known
        # official app binary, never its auth/config directories. An explicit
        # administrator setting always wins, including an invalid/disabled one.
        if sys.platform == "darwin":
            return shutil.which("/Applications/ChatGPT.app/Contents/Resources/codex")
        return None

    def _snapshot(self, available=True):
        return {
            "available": available,
            "busy": self._chat_busy,
            "connected": self._connected,
            "login_status": self._login_status,
            "message": _MESSAGES[self._login_status],
            "login_id": self._login_id,
            "account_type": "chatgpt" if self._connected else None,
            "plan_type": self._plan,
            "chat_available": self._connected and self._chat_available,
            "chat_message": "已验证新闻隔离沙箱。" if self._chat_available else CHAT_BLOCKED,
        }

    def _prepare_home(self):
        home = Path(getattr(self._config, "AI_CODEX_HOME", Path.home() / ".claw" / "codex-news")).expanduser()
        # Do not use, modify permissions on, or read shared personal Codex homes.
        shared = {Path.home() / ".codex", Path.home()}
        inherited = os.environ.get("CODEX_HOME")
        if inherited:
            shared.add(Path(inherited).expanduser())
        if (not home.is_absolute() or home in shared or home.resolve() != home
                or any(home == p or home in p.parents for p in shared)):
            raise BridgeError("Unsafe credential directory")
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not home.is_dir() or home.is_symlink() or home.stat().st_uid != os.getuid():
            raise BridgeError("Unsafe credential directory")
        home.chmod(0o700)
        return home

    async def _start(self):
        if (self._process is not None and self._process.returncode is None
                and self._reader is not None and not self._reader.done()):
            return
        await self.close()
        executable = self._executable()
        if not executable:
            self._login_status = "unavailable"
            raise BridgeError("Codex CLI unavailable")
        home = self._prepare_home()
        self._workspace = tempfile.TemporaryDirectory(prefix="claw-codex-empty-")
        os.chmod(self._workspace.name, 0o700)
        # No inherited API keys, shared home config, plugins, or shell initialization.
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT") if key in os.environ}
        # The CLI transport needs the same administrator-configured proxy as the
        # browser. Rust does not discover macOS SystemConfiguration proxies. Pass
        # only proxy settings, never API keys or personal CLI HOME/config. This
        # affects the app-server transport; the verified tool profile still denies
        # network. Do not print these values (proxy URLs may contain credentials).
        proxies = getproxies()
        for name in ("http", "https", "all"):
            value = proxies.get(name)
            if isinstance(value, str) and value:
                env[name.upper() + "_PROXY"] = value
        if isinstance(proxies.get("no"), str):
            env["NO_PROXY"] = proxies["no"]
        env.update({"CODEX_HOME": str(home), "HOME": self._workspace.name,
                    "USERPROFILE": self._workspace.name})
        # Global flags are applied before any thread/config discovery. Named
        # profile selects only the empty directory; its actual resolved policy
        # must still be confirmed by thread/start before sending news.
        overrides = {
            "cli_auth_credentials_store": "file", "forced_login_method": "chatgpt",
            "model_provider": "openai", "approval_policy": "never",
            "web_search": "disabled", "project_doc_max_bytes": 0,
            "apps._default.enabled": False, "mcp_servers": {}, "plugins": {},
            "features.skip_host_skill_discovery": True,
            "default_permissions": _PROFILE,
            **{"features." + name: False for name in _DISABLED_FEATURES},
        }
        args = [executable]
        for key, value in overrides.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        # CLI -c splits the key on dots; it does NOT unquote TOML path keys.
        # Supply filesystem as a TOML value so quotes/spaces/dots in absolute
        # paths survive parsing. A dotted quoted key makes the real CLI exit.
        args.extend(["-c", "permissions." + _PROFILE + "={ filesystem = { " +
                     json.dumps(str(Path(self._workspace.name).resolve())) + ' = "read" } }'])
        args.extend(["app-server", "--listen", "stdio://"])
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=self._workspace.name, env=env,
                limit=1024 * 1024,
            )
            self._reader = asyncio.create_task(self._read_messages(self._process))
            await self._rpc("initialize", {
                "clientInfo": {"name": "claw_news_bridge", "title": "Claw News", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            await self._send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def _send(self, message):
        async with self._write_lock:
            process = self._process
            if process is None or process.returncode is not None or process.stdin is None:
                raise BridgeError("Codex process unavailable")
            try:
                process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                async with asyncio.timeout(self._timeout):
                    await process.stdin.drain()
            except (OSError, asyncio.TimeoutError):
                raise BridgeError("Codex transport failed") from None

    async def _rpc(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        # Register before writing: responses/notifications may arrive immediately.
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            async with asyncio.timeout(self._timeout):
                return await future
        except asyncio.TimeoutError:
            raise BridgeError("Codex request timed out") from None
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume an EOF exception even if writing failed first.

    async def _read_messages(self, process):
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise BridgeError("Invalid protocol")
                if "method" in message:
                    if "id" in message:
                        # Never approve tools, commands, file reads, or token refresh requests.
                        self._reject_active_turns()
                        await self._send({"id": message["id"], "error": {
                            "code": -32601, "message": "Client requests are disabled"}})
                    else:
                        self._notify(message.get("method"), message.get("params"))
                elif isinstance(message.get("id"), int):
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message or not isinstance(message.get("result"), dict):
                            future.set_exception(BridgeError("Codex request failed"))
                        else:
                            future.set_result(message["result"])
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(BridgeError("Codex process disconnected"))
            self._connected = False
            self._plan = None
            self._login_status = "error"
            self._chat_available = False
            self._reject_active_turns()

    def _notify(self, method, params):
        if not isinstance(params, dict):
            return
        self._turn_notification(method, params)
        if method == "account/login/completed":
            login_id = _login_id(params.get("loginId"))
            if login_id:
                success = params.get("success") is True
                self._completions[login_id] = success
                while len(self._completions) > 16:
                    self._completions.popitem(last=False)
                if login_id == self._login_id:
                    self._login_status = "completed" if success else "failed"
                    self._login_completed_until = time.monotonic() + 15.0 if success else 0.0
                    self._login_id = None
        elif method == "account/updated":
            self._connected = params.get("authMode") == "chatgpt"
            plan = params.get("planType")
            self._plan = plan if isinstance(plan, str) and plan in _PLANS else None
            if self._connected:
                self._login_status = "connected"
            elif self._login_status not in {"pending", "completed", "cancelled", "failed"}:
                self._login_status = "disconnected"

    async def _failure(self):
        await self.close()
        self._login_status = "error" if self._executable() else "unavailable"
        return self._snapshot(self._executable() is not None)

    async def status(self):
        """Check account/capability, or serve a safe cached snapshot while chatting."""
        if self._chat_busy:
            return self._snapshot(self._executable() is not None)
        async with self._lock:
            try:
                await self._start()
                data = await self._rpc("account/read", {"refreshToken": False})
                account = data.get("account")
                self._connected = isinstance(account, dict) and account.get("type") == "chatgpt"
                plan = account.get("planType") if isinstance(account, dict) else None
                self._plan = plan if isinstance(plan, str) and plan in _PLANS else None
                if self._connected:
                    self._login_status = "connected"
                    self._login_id = None
                    if not self._chat_checked:
                        await self._probe_chat()
                elif self._login_status == "completed":
                    if time.monotonic() >= self._login_completed_until:
                        self._login_status = "failed"
                elif self._login_status not in {"pending", "failed", "cancelled"}:
                    self._login_status = "disconnected"
                return self._snapshot()
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception:
                return await self._failure()

    async def list_models(self):
        """Read the CLI catalog only; never start a thread, turn or login."""
        failure = {"ok": False, "models": [], "message": "模型目录读取失败，请稍后刷新；不会修改已保存配置或账号授权。"}
        if self._chat_busy or self._lock.locked():
            return {**failure, "message": "账号服务正在处理请求，请稍后刷新模型目录。"}
        async with self._lock:
            try:
                async with asyncio.timeout(self._timeout):
                    await self._start()
                    account_result = await self._rpc("account/read", {"refreshToken": False})
                    account = account_result.get("account")
                    if not isinstance(account, dict) or account.get("type") != "chatgpt":
                        return {**failure, "message": "请先完成 OpenAI 账号授权，再读取模型目录。"}
                    models = await self._read_model_catalog()
                    return {"ok": True, "models": models, "message": "目录来自 Codex；具体模型权限、额度与分析能力仍以连接测试为准。"}
            except asyncio.CancelledError:
                raise
            except Exception:
                # A catalog error must not logout/cancel a user or clear a valid
                # connection. In particular do not use the login failure handler.
                return failure

    async def _read_model_catalog(self):
        """Read owned display/capability fields under the caller's bridge lock."""
        models, seen, cursors = [], set(), set()
        cursor = None
        for _ in range(10):
            params = {"limit": 50, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            page = await self._rpc("model/list", params)
            entries = page.get("data")
            if not isinstance(entries, list):
                raise BridgeError("Invalid model catalog")
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("hidden") is True:
                    continue
                model = entry.get("model") or entry.get("id")
                if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", model):
                    continue
                if model in seen:
                    continue
                modalities = entry.get("inputModalities")
                if isinstance(modalities, list) and "text" not in modalities:
                    continue
                label = entry.get("displayName")
                if not isinstance(label, str) or not label.strip() or len(label) > 160 or any(ord(c) < 32 for c in label):
                    label = model
                efforts = []
                raw_efforts = entry.get("supportedReasoningEfforts")
                if isinstance(raw_efforts, list):
                    for option in raw_efforts:
                        effort = option.get("reasoningEffort") if isinstance(option, dict) else None
                        if isinstance(effort, str) and effort in _REASONING_EFFORTS and effort not in efforts:
                            efforts.append(effort)
                default_effort = entry.get("defaultReasoningEffort")
                models.append({"id": model, "label": label, "is_default": entry.get("isDefault") is True,
                               "reasoning_efforts": efforts,
                               "default_reasoning_effort": default_effort if default_effort in efforts else ""})
                seen.add(model)
                if len(models) > 500:
                    raise BridgeError("Model catalog too large")
            cursor = page.get("nextCursor")
            if cursor is None:
                return models
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
                raise BridgeError("Invalid model pagination")
            cursors.add(cursor)
        raise BridgeError("Model pagination limit reached")

    async def start_login(self, login_type="chatgpt"):
        """Explicit user action only; returns allowlisted browser/device fields."""
        if login_type not in {"chatgpt", "chatgptDeviceCode"}:
            raise ValueError("Unsupported login type")
        async with self._lock:
            try:
                await self._start()
                if self._login_id:
                    await self._rpc("account/login/cancel", {"loginId": self._login_id})
                self._login_id = None
                self._completions.clear()
                self._login_status = "pending"
                data = await self._rpc("account/login/start", {"type": login_type})
                login_id = _login_id(data.get("loginId"))
                field = "authUrl" if login_type == "chatgpt" else "verificationUrl"
                url = _auth_url(data.get(field))
                if not login_id or not url or data.get("type") != login_type:
                    raise BridgeError("Invalid login result")
                extra = {"type": login_type, "auth_url": url if login_type == "chatgpt" else None,
                         "verification_url": url if login_type == "chatgptDeviceCode" else None}
                if login_type == "chatgptDeviceCode":
                    code = data.get("userCode")
                    if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9-]{4,32}", code):
                        raise BridgeError("Invalid device code")
                    extra["user_code"] = code
                self._login_id = login_id
                if login_id in self._completions:
                    success = self._completions.pop(login_id)
                    self._login_status = "completed" if success else "failed"
                    self._login_completed_until = time.monotonic() + 15.0 if success else 0.0
                    self._login_id = None
                return {**self._snapshot(), **extra, "login_id": login_id}
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception:
                return await self._failure()

    async def cancel_login(self):
        async with self._lock:
            try:
                if self._login_id:
                    await self._rpc("account/login/cancel", {"loginId": self._login_id})
                self._login_id = None
                self._login_status = "cancelled"
                return self._snapshot(self._executable() is not None)
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception:
                return await self._failure()

    async def logout(self):
        async with self._lock:
            try:
                await self._start()
                await self._rpc("account/logout", {})
                self._connected = False
                self._plan = None
                self._login_id = None
                self._completions.clear()
                self._login_status = "disconnected"
                return self._snapshot()
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception:
                return await self._failure()

    def _check_permission_config(self, config):
        """No inheritance, platform defaults, workspace expansion, or extra path rules."""
        profiles = config.get("permissions")
        profile = profiles.get(_PROFILE) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict) or set(profile) - {
                "description", "extends", "workspace_roots", "filesystem", "network"}:
            raise BridgeError("Cannot verify permission profile")
        if (profile.get("extends") is not None or profile.get("workspace_roots") not in (None, [])
                or profile.get("network") is not None):
            raise BridgeError("Permission inheritance or network is not allowed")
        filesystem = profile.get("filesystem")
        cwd = str(Path(self._workspace.name).resolve())
        if (not isinstance(filesystem, dict)
                or set(filesystem) - {cwd, "glob_scan_max_depth"}
                or filesystem.get(cwd) != "read"
                or filesystem.get("glob_scan_max_depth") is not None):
            raise BridgeError("Only the empty directory may be readable")

    async def _check_permission_available(self):
        cursor, cursors, matches = None, set(), []
        for _ in range(10):
            page = await self._rpc("permissionProfile/list", {
                "cwd": str(Path(self._workspace.name).resolve()), "limit": 100, "cursor": cursor})
            entries = page.get("data")
            if not isinstance(entries, list):
                raise BridgeError("Permission profiles unavailable")
            matches.extend(entry for entry in entries if isinstance(entry, dict) and entry.get("id") == _PROFILE)
            cursor = page.get("nextCursor")
            if cursor is None:
                if len(matches) != 1 or matches[0].get("allowed") is not True:
                    raise BridgeError("Required permission profile not allowed")
                return
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
                raise BridgeError("Invalid permission profile page")
            cursors.add(cursor)
        raise BridgeError("Permission profile pagination limit")

    async def _check_effective_config(self):
        result = await self._rpc("config/read", {
            "cwd": str(Path(self._workspace.name).resolve()), "includeLayers": False})
        config = result.get("config")
        if not isinstance(config, dict):
            raise BridgeError("Cannot verify effective configuration")
        features = config.get("features")
        if not isinstance(features, dict) or any(
                features.get(name) is not False for name in _DISABLED_FEATURES):
            raise BridgeError("Cannot verify disabled features")
        if (config.get("web_search") != "disabled"
                or config.get("mcp_servers") != {}
                or config.get("plugins") != {}
                or config.get("model_provider") != "openai"
                or config.get("approval_policy") != "never"
                or config.get("default_permissions") != _PROFILE
                or config.get("project_doc_max_bytes") != 0
                or config.get("model_providers", {}) != {}):
            raise BridgeError("Unsafe effective configuration")
        apps = config.get("apps")
        if (not isinstance(apps, dict) or set(apps) != {"_default"}
                or not isinstance(apps["_default"], dict)
                or apps["_default"].get("enabled") is not False
                or features.get("skip_host_skill_discovery") is not True):
            raise BridgeError("Apps or host skill discovery are not disabled")
        self._check_permission_config(config)
        await self._check_permission_available()
        # Config echo alone is insufficient: older CLI versions may preserve an
        # unknown key without implementing it. Require canonical runtime flags.
        required = {"shell_tool", "view_image", "multi_agent", "apps", "plugins"}
        cursor = None
        for _ in range(10):
            page = await self._rpc("experimentalFeature/list", {"limit": 100, "cursor": cursor})
            entries = page.get("data")
            if not isinstance(entries, list):
                raise BridgeError("Cannot verify runtime feature flags")
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") in required:
                    if entry.get("enabled") is not False:
                        raise BridgeError("Unsafe runtime feature")
                    required.remove(entry["name"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str):
                raise BridgeError("Invalid feature page")
        if required or cursor:
            raise BridgeError("Required runtime feature controls unavailable")

    async def _safe_thread(self, system="", model=None, reasoning_effort=""):
        await self._check_effective_config()
        cwd = str(Path(self._workspace.name).resolve())
        if any(Path(cwd).iterdir()):
            raise BridgeError("Workspace is not empty")
        params = {
            "cwd": cwd, "approvalPolicy": "never", "ephemeral": True,
            "modelProvider": "openai", "serviceName": "claw_news",
            "permissions": _PROFILE, "runtimeWorkspaceRoots": [],
            "allowProviderModelFallback": False,
            # Not a security boundary; OS/CLI permissions below remain mandatory.
            "baseInstructions": "Analyze only the supplied financial news as text. Do not use tools.",
            "developerInstructions": system,
        }
        selected_model = getattr(self._config, "AI_CODEX_MODEL", "") if model is None else model
        if selected_model:
            params["model"] = selected_model
        if reasoning_effort:
            params["config"] = {"model_reasoning_effort": reasoning_effort}
        try:
            data = await self._rpc("thread/start", params)
        except BaseException:
            # Unknown thread ID after timeout/cancellation: tear down in-memory work.
            await self.close()
            raise
        thread = data.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            await self.close()
            raise BridgeError("Invalid thread response")
        active_profile = data.get("activePermissionProfile")
        if (data.get("sandbox") != {"type": "readOnly", "networkAccess": False}
                or not isinstance(active_profile, dict)
                or active_profile.get("id") != _PROFILE or active_profile.get("extends") is not None
                or data.get("runtimeWorkspaceRoots") != []
                or data.get("approvalPolicy") != "never" or data.get("cwd") != cwd
                or data.get("modelProvider") != "openai"
                or data.get("instructionSources") != []
                or thread.get("ephemeral") is not True
                or any(Path(cwd).iterdir())):
            # No turn/news has been sent. Delete accidental persistent empty threads.
            await self._cleanup_thread(thread_id, ephemeral=thread.get("ephemeral") is True)
            raise BridgeError("Restricted permission profile was not confirmed")
        if ((selected_model and data.get("model") != selected_model)
                or (reasoning_effort and data.get("reasoningEffort") != reasoning_effort)):
            await self._cleanup_thread(thread_id)
            raise BridgeError("Requested model or reasoning effort was not confirmed")
        return thread_id

    async def _cleanup_thread(self, thread_id, *, ephemeral=True, turn_id=None):
        try:
            if turn_id:
                await self._rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            await self._rpc("thread/unsubscribe", {"threadId": thread_id})
            if not ephemeral:
                await self._rpc("thread/delete", {"threadId": thread_id})
        except Exception:
            # Closing the process destroys ephemeral in-memory threads and tools.
            await self.close()

    async def _probe_chat(self):
        self._chat_available = False
        try:
            thread_id = await self._safe_thread()
            await self._cleanup_thread(thread_id)
            self._chat_available = self._process is not None
        except Exception:
            # Config/schema incompatibility must not turn a valid login into a
            # claimed model connection, nor send a test AI request automatically.
            self._chat_available = False
        self._chat_checked = True

    def _reject_active_turns(self):
        if self._turns:
            self._chat_available = False
        for state in self._turns.values():
            state["rejected"] = True
            if not state["done"].done():
                state["done"].set_result(False)

    def _turn_notification(self, method, params):
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        state = self._turns.get(thread_id)
        if state is None:
            return
        if isinstance(params.get("turnId"), str):
            state["event_turn_ids"].add(params["turnId"])
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                return
            kind = item.get("type")
            if kind not in {"userMessage", "agentMessage", "reasoning"}:
                self._reject_active_turns()
                return
            if method == "item/completed" and kind == "agentMessage":
                item_id, text = item.get("id"), item.get("text")
                if not isinstance(item_id, str) or not isinstance(text, str):
                    self._reject_active_turns()
                    return
                state["messages"][item_id] = text
                if sum(len(t) for t in state["messages"].values()) > _MAX_TEXT:
                    self._reject_active_turns()
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            state["completed_id"] = turn.get("id")
            if not state["done"].done():
                state["done"].set_result(turn.get("status") == "completed")
        elif method == "error":
            # The CLI owns bounded transport retries; a recoverable stream event
            # is not a completed failure. The overall turn timeout still applies.
            if params.get("willRetry") is not True:
                self._reject_active_turns()
        elif method == "thread/closed":
            self._reject_active_turns()

    async def chat(self, prompt: str, system: str = "", model: str | None = None,
                   reasoning_effort: str = "") -> str | None:
        """One ephemeral text-only turn, after strict server-side sandbox acknowledgement."""
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_TEXT
                or not isinstance(reasoning_effort, str)
                or (reasoning_effort and reasoning_effort not in _REASONING_EFFORTS)):
            return None
        async with self._lock:
            self._chat_busy = True
            thread_id = turn_id = None
            state = None
            completed = False
            try:
                await self._start()
                account = (await self._rpc("account/read", {"refreshToken": False})).get("account")
                self._connected = isinstance(account, dict) and account.get("type") == "chatgpt"
                if not self._connected:
                    self._login_status = "disconnected"
                    return None
                self._login_status = "connected"
                selected_model = getattr(self._config, "AI_CODEX_MODEL", "") if model is None else model
                if reasoning_effort:
                    async with asyncio.timeout(self._timeout):
                        catalog = await self._read_model_catalog()
                    selected = next((entry for entry in catalog if
                                     (entry["id"] == selected_model if selected_model else entry["is_default"])), None)
                    if selected is None or reasoning_effort not in selected["reasoning_efforts"]:
                        raise BridgeError("Reasoning effort is not supported by this model")
                    # Pin the catalog default when validating an explicit effort.
                    selected_model = selected["id"]
                # Revalidate each new thread; a cached status is not authorization.
                thread_id = await self._safe_thread(system, selected_model, reasoning_effort)
                self._chat_available = self._chat_checked = True
                state = {"messages": OrderedDict(), "done": asyncio.get_running_loop().create_future(),
                         "completed_id": None, "rejected": False, "event_turn_ids": set()}
                self._turns[thread_id] = state  # BEFORE RPC: completion can race its response.
                params = {
                    "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                    "cwd": str(Path(self._workspace.name).resolve()),
                    "approvalPolicy": "never", "permissions": _PROFILE,
                    "runtimeWorkspaceRoots": [],
                }
                if reasoning_effort:
                    params["effort"] = reasoning_effort
                result = await self._rpc("turn/start", params)
                turn = result.get("turn")
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                if not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", turn_id):
                    raise BridgeError("Invalid turn response")
                async with asyncio.timeout(self._turn_timeout):
                    success = await state["done"]
                completed = state["completed_id"] == turn_id
                if (not success or state["rejected"] or not completed
                        or state["event_turn_ids"] - {turn_id}):
                    return None
                return "\n".join(state["messages"].values()).strip() or None
            except asyncio.CancelledError:
                # Caller cancellation must terminate pending work, not let it continue silently.
                if thread_id and turn_id:
                    await self._cleanup_thread(thread_id, turn_id=turn_id)
                await self.close()
                raise
            except Exception:
                self._chat_available = False
                # If start timed out, the server may have accepted an unknown
                # thread/turn ID. Killing the transport cancels that uncertainty.
                if (thread_id and not turn_id) or self._process is None:
                    await self.close()
                return None
            finally:
                try:
                    if thread_id:
                        self._turns.pop(thread_id, None)
                        if state is not None and not state["done"].done():
                            state["done"].cancel()
                        if self._process is not None:
                            await self._cleanup_thread(
                                thread_id, turn_id=turn_id if not completed else None)
                finally:
                    self._chat_busy = False

    async def close(self):
        """Stop transport/callback server; official credentials remain on disk."""
        async with self._close_lock:
            process, reader = self._process, self._reader
            self._process = self._reader = None
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(BridgeError("Codex process closed"))
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            if self._workspace is not None:
                self._workspace.cleanup()
                self._workspace = None
            self._connected = False
            self._plan = None
            self._login_id = None
            self._completions.clear()
            self._login_status = "disconnected"
            self._chat_available = self._chat_checked = False
            self._reject_active_turns()


codex_bridge = CodexBridge()
