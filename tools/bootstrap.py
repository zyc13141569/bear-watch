#!/usr/bin/env python3
"""
bear-watch 一键部署脚本。

它替你做完 README 里的第 1、3、4、6 步：
  1. 在 GitHub 上创建仓库（如果已存在就直接用）
  3. 写入 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / DEEPSEEK_API_KEY 三个 Secret
  4. 把 Actions 的工作流权限改成 Read and write
  6. 触发第一次采集并立即推送 Telegram 做验证

第 2 步（建 Telegram 机器人）必须你自己在手机上做，脚本代替不了。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
在解压后的项目根目录下运行：

    pip install requests pynacl
    python tools/bootstrap.py

然后按提示粘贴凭证。所有输入都用隐藏输入，不回显、不写盘、不进 shell 历史。

也可以走环境变量（适合非交互场景）：

    export GH_TOKEN=github_pat_...
    export GH_OWNER=yourname
    export GH_REPO=bear-watch
    export TELEGRAM_BOT_TOKEN=...
    export TELEGRAM_CHAT_ID=...
    export DEEPSEEK_API_KEY=sk-...
    python tools/bootstrap.py --yes

────────────────────────────────────────────────────────────────
需要什么样的 token
────────────────────────────────────────────────────────────────
两个选择，二选一：

A) 经典 token（最省事）
   github.com/settings/tokens → Generate new token (classic)
   勾选 scope: `repo` 和 `workflow`
   有效期建议 7 天 —— 部署完就过期，不用记得撤销

B) 细粒度 token（权限更小，但要先手动建空仓库）
   先去 github.com/new 建一个空的 public 仓库（不要勾 Add a README）
   然后 github.com/settings/personal-access-tokens/new
   Repository access → Only select repositories → 只选那一个
   Repository permissions 勾这五项，全给 Read and write：
     Administration / Contents / Workflows / Secrets / Actions

⚠️ `workflow`（或 Workflows: Read and write）这一项最容易漏。
   漏了的话推送 .github/workflows/ 会被 GitHub 直接拒绝，报
   "refusing to allow a Personal Access Token to create or update workflow"。

部署完成后立刻去把这个 token 删掉。每周日给 Claude 用的那个 PAT 是另一个，
只需要 Contents: Read and write，权限小得多。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：pip install requests pynacl")

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"


def ok(m):    print(f"{G}✅{X} {m}")
def warn(m):  print(f"{Y}⚠️ {X} {m}")
def die(m):   print(f"{R}❌ {m}{X}"); sys.exit(1)
def step(m):  print(f"\n{B}── {m} ──{X}")


def ask(prompt: str, env: str, secret: bool = True, required: bool = True) -> str:
    v = os.environ.get(env, "").strip()
    if v:
        print(f"   {prompt}：{G}已从环境变量 {env} 读取{X}")
        return v
    v = (getpass.getpass(f"   {prompt}：") if secret else input(f"   {prompt}：")).strip()
    if not v and required:
        die(f"{prompt} 不能为空")
    return v


def gh(token: str, method: str, path: str, **kw):
    r = requests.request(
        method, API + path, timeout=60,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        **kw)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="不做交互确认")
    ap.add_argument("--private", action="store_true",
                    help="建成私有仓库（注意：这样 Claude 就读不到 REPORT.md 了）")
    ap.add_argument("--skip-secrets", action="store_true", help="不写 Secrets")
    ap.add_argument("--skip-run", action="store_true", help="不触发首次运行")
    args = ap.parse_args()

    print(f"\n{B}🐻 bear-watch 一键部署{X}")
    print("   所有输入都是隐藏的，不回显、不写盘、不进 shell 历史。\n")

    step("1/6 GitHub 凭证")
    token = ask("GitHub token（classic 需 repo+workflow）", "GH_TOKEN")
    r = gh(token, "GET", "/user")
    if r.status_code != 200:
        die(f"token 无效：HTTP {r.status_code} {r.text[:200]}")
    me = r.json()["login"]
    ok(f"已登录为 {B}{me}{X}")

    owner = os.environ.get("GH_OWNER", "").strip() or me
    repo = ask("仓库名（回车用 bear-watch）", "GH_REPO", secret=False, required=False) or "bear-watch"
    full = f"{owner}/{repo}"

    step("2/6 创建仓库")
    r = gh(token, "GET", f"/repos/{full}")
    if r.status_code == 200:
        info = r.json()
        warn(f"{full} 已存在（{'private' if info['private'] else 'public'}），直接使用")
        if info["private"] and not args.private:
            warn("它是私有的 —— Claude 每周日将无法通过 raw 链接读 REPORT.md。"
                 "建议去 Settings 改成 public，或接受 CC 段改为手动模式。")
        default_branch = info.get("default_branch") or "main"
    elif r.status_code == 404:
        r = gh(token, "POST", "/user/repos", json={
            "name": repo, "private": bool(args.private),
            "description": "美股熊市概率监控 · 每周日 Telegram 推送",
            "auto_init": False, "has_issues": False, "has_wiki": False,
        })
        if r.status_code not in (200, 201):
            die(f"创建仓库失败：HTTP {r.status_code} {r.text[:300]}\n"
                f"   如果是 403，多半是 token 缺 repo / Administration 权限。")
        ok(f"已创建 {B}{full}{X}（{'private' if args.private else 'public'}）")
        default_branch = "main"
        time.sleep(2)
    else:
        die(f"查询仓库失败：HTTP {r.status_code} {r.text[:200]}")

    step("3/6 推送代码")
    tmp = tempfile.mkdtemp(prefix="bw-")
    try:
        for item in os.listdir(ROOT):
            if item in (".git", "__pycache__", ".venv", "venv"):
                continue
            s, d = os.path.join(ROOT, item), os.path.join(tmp, item)
            (shutil.copytree if os.path.isdir(s) else shutil.copy2)(
                s, d, **({"ignore": shutil.ignore_patterns("__pycache__", "*.pyc", "cache")}
                         if os.path.isdir(s) else {}))
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        url = f"https://x-access-token:{token}@github.com/{full}.git"

        def run(*a, check=True):
            p = subprocess.run(a, cwd=tmp, env=env, capture_output=True, text=True)
            if check and p.returncode != 0:
                msg = (p.stderr or p.stdout).replace(token, "***")
                if "workflow" in msg and "refusing" in msg:
                    die("推送被拒：token 缺少 workflow 权限。\n"
                        "   classic token 请勾 `workflow` scope；\n"
                        "   fine-grained token 请给 Workflows: Read and write。")
                die(f"git {' '.join(a[1:3])} 失败：\n{msg[:600]}")
            return p

        run("git", "init", "-q", "-b", default_branch)
        run("git", "config", "user.name", "bear-watch-setup")
        run("git", "config", "user.email", "setup@users.noreply.github.com")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "🐻 initial commit: bear-watch")
        run("git", "remote", "add", "origin", url)
        p = run("git", "push", "-u", "origin", default_branch, check=False)
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).replace(token, "***")
            if "fetch first" in msg or "non-fast-forward" in msg:
                warn("远端已有内容，改为强制覆盖")
                run("git", "push", "-f", "origin", default_branch)
            elif "refusing" in msg and "workflow" in msg:
                die("推送被拒：token 缺少 workflow 权限（见上文说明）。")
            else:
                die(f"push 失败：\n{msg[:600]}")
        n = len(run("git", "ls-files").stdout.strip().splitlines())
        ok(f"已推送 {n} 个文件到 {default_branch} 分支")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    step("4/6 打开 Actions 写权限")
    r = gh(token, "PUT", f"/repos/{full}/actions/permissions/workflow",
           json={"default_workflow_permissions": "write",
                 "can_approve_pull_request_reviews": False})
    if r.status_code in (204, 200):
        ok("工作流权限已设为 Read and write")
    else:
        warn(f"设置失败（HTTP {r.status_code}），请手动去 Settings → Actions → General "
             f"勾选 Read and write permissions")

    step("5/6 写入 Secrets")
    if args.skip_secrets:
        warn("已跳过")
    else:
        try:
            from nacl import encoding, public
        except ImportError:
            die("缺少 PyNaCl：pip install pynacl")
        r = gh(token, "GET", f"/repos/{full}/actions/secrets/public-key")
        if r.status_code != 200:
            die(f"拿不到仓库公钥：HTTP {r.status_code} {r.text[:200]}\n"
                f"   token 需要 Secrets: Read and write 权限。")
        pk = r.json()
        sealed = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder))

        print("   （下面三项直接回车可跳过，之后自己去 GitHub 网页填）")
        wanted = {
            "TELEGRAM_BOT_TOKEN": ask("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", required=False),
            "TELEGRAM_CHAT_ID":  ask("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID", required=False),
            "DEEPSEEK_API_KEY":  ask("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", required=False),
            "FRED_API_KEY":      ask("FRED_API_KEY（可选，回车跳过）", "FRED_API_KEY", required=False),
        }
        for name, val in wanted.items():
            if not val:
                continue
            enc = base64.b64encode(sealed.encrypt(val.encode())).decode()
            r = gh(token, "PUT", f"/repos/{full}/actions/secrets/{name}",
                   json={"encrypted_value": enc, "key_id": pk["key_id"]})
            (ok if r.status_code in (201, 204) else warn)(
                f"Secret {name} " + ("已写入" if r.status_code in (201, 204)
                                     else f"写入失败 HTTP {r.status_code}"))

        # 顺手验证 Telegram 是否真的能发消息 —— 早点发现比周日发现好
        bt, cid = wanted["TELEGRAM_BOT_TOKEN"], wanted["TELEGRAM_CHAT_ID"]
        if bt and cid:
            try:
                rr = requests.post(f"https://api.telegram.org/bot{bt}/sendMessage", timeout=30,
                                   json={"chat_id": cid,
                                         "text": "🐻 bear-watch 部署成功，通道已打通。"
                                                 "第一份周报将在本周日送达。"})
                if rr.status_code == 200 and rr.json().get("ok"):
                    ok("Telegram 测试消息已发出 —— 去看看收到没有")
                else:
                    warn(f"Telegram 测试失败：{rr.text[:200]}\n"
                         f"      最常见原因：你还没有主动给机器人发过第一条消息。")
            except Exception as e:
                warn(f"Telegram 测试异常（不影响部署）：{e!r}")

    step("6/6 触发首次运行")
    if args.skip_run:
        warn("已跳过")
    else:
        go = args.yes or (input("   现在跑一次完整采集并推送周报？[Y/n] ").strip().lower()
                          in ("", "y", "yes"))
        if go:
            time.sleep(3)
            r = gh(token, "POST",
                   f"/repos/{full}/actions/workflows/weekly-collect.yml/dispatches",
                   json={"ref": default_branch, "inputs": {"send_now": "true"}})
            if r.status_code == 204:
                ok("已触发。约 1-2 分钟后应收到 Telegram 周报。")
                print(f"   运行日志：https://github.com/{full}/actions")
            else:
                warn(f"触发失败 HTTP {r.status_code} {r.text[:200]}\n"
                     f"      手动去 Actions 页面点 Run workflow 也行。")

    print(f"\n{B}{G}部署完成{X}")
    print(f"   仓库　　　 https://github.com/{full}")
    print(f"   运行记录　 https://github.com/{full}/actions")
    print(f"   报告文档　 https://github.com/{full}/blob/{default_branch}/REPORT.md")
    print(f"   raw 链接　 https://raw.githubusercontent.com/{full}/{default_branch}/REPORT.md")
    print(f"\n{Y}还剩两件事：{X}")
    print(f"   1. 现在就去把刚才这个部署用的 token 删掉（它权限很大，用完就没价值了）")
    print(f"   2. 另外建一个 fine-grained PAT，只给这一个仓库的 Contents: Read and write，")
    print(f"      连同上面的 raw 链接一起发给 Claude，用来开启每周日的 CC 解读任务。\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
