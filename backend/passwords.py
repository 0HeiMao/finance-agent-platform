"""弱口令黑名单与密码强度（④）。

为什么要有它：`Credentials` 只给了 6 个字符的长度下限，`Password1`、`finance2026`、
`12345678` 全都能注册成功。而这个系统的账号能看到整库财务明细，弱口令就是最省事的那条
攻击路径——爆破脚本要试的第一批就是这些词。

判据分三层（**只在注册/改密码时用**，登录不校验，否则老账号会被自己的新规则挡在门外）：

1. **整串归一化后命中黑名单**：小写 → 常见 l33t 替换还原（`0→o`、`1→i`、`3→e`、
   `4→a`、`5→s`、`7→t`、`@→a`、`$→s`）→ 去掉所有非字母数字 → 去掉结尾的连续数字。
   于是 `P@ssw0rd!` → `password`、`Finance2026` → `finance`，都算弱口令。
2. **按分隔符切出的词**命中短词黑名单：`finance_2026` 的 `finance`、`agent_password_2026`
   的 `password` 都会被拦；而 `pytest-login-2026` 的 `pytest` 不是词，不会被误伤——
   这就是为什么短词（`test`、`demo`、`root`）只在**分词**这一层比，不做子串匹配。
3. **子串**命中「一眼可猜」的长词：`password`、`qwerty`、`iloveyou`、`letmein`、
   `changeme`、`123456789`——正常密码不会把这些词嵌在中间，嵌了就是弱口令。

另外还挡了三种「不是词但一样弱」的形态：同一个字符重复、纯数字、键盘顺序
（`qwertyuiop` / `asdfghjkl` / `zxcvbnm` / `1234567890` / `abcdefghij`）。
"""
from __future__ import annotations

import re

# 整串归一化后命中即拒绝（配合 l33t 还原与去尾数字，覆盖面比列出来的宽）
WEAK_EXACT = frozenset({
    "password", "passwd", "pass", "123456", "1234567", "12345678", "123456789",
    "qwerty", "qwertyuiop", "asdfghjkl", "zxcvbnm", "abc123", "abcd1234",
    "iloveyou", "letmein", "welcome", "monkey", "dragon", "master", "shadow",
    "sunshine", "princess", "football", "baseball", "superman", "trustno1",
    "changeme", "secret", "default", "administrator", "admin", "root", "guest",
    "test", "demo", "finance", "company", "manager", "accounting", "letmein123",
})

# 分词后按「词」比对，不做子串匹配（避免 pytest → test 这类误伤）
WEAK_TOKENS = frozenset({
    "password", "passwd", "admin", "administrator", "root", "guest", "test",
    "demo", "finance", "accounting", "secret", "changeme", "letmein", "qwerty",
    "abc123", "123456", "12345678", "welcome", "default",
})

# 子串匹配：这些词出现在密码中间也一样弱（正常密码不会这么拼）
WEAK_SUBSTRINGS = (
    "password", "passwd", "qwerty", "iloveyou", "letmein", "changeme", "123456789",
)

KEYBOARD_RUNS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890", "0987654321", "abcdefghij")

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def min_length() -> int:
    """长度下限读配置（默认 10）：部署方要更严改环境变量就行，不用改代码。"""
    from backend.config import get_settings

    try:
        return max(6, int(get_settings().password_min_length))
    except Exception:  # pragma: no cover - 配置读不出来时退回默认，不挡注册
        return 10


def normalize(password: str) -> str:
    """归一化：小写 → l33t 还原 → 去非字母数字 → 去结尾数字。给黑名单比对用。"""
    lowered = (password or "").lower().translate(_LEET)
    compact = _NON_ALNUM.sub("", lowered)
    return compact.rstrip("0123456789")


def tokens(password: str) -> list[str]:
    """按非字母数字切词（`finance_2026` → `['finance', '2026']`）。"""
    return [item for item in _NON_ALNUM.split((password or "").lower()) if item]


def problems(password: str, username: str | None = None) -> list[str]:
    """返回所有不满足的规则（中文说明，直接可以回给前端）。空列表 = 合格。"""
    found: list[str] = []
    raw = password or ""
    limit = min_length()
    if len(raw) < limit:
        found.append(f"密码至少 {limit} 个字符（当前 {len(raw)} 个）")
    if raw != raw.strip():
        found.append("密码首尾不要留空格")
    if len(set(raw)) == 1:
        found.append("密码不能是同一个字符重复")
    if raw.isdigit():
        found.append("密码不能全是数字")

    normalized = normalize(raw)
    if normalized in WEAK_EXACT:
        found.append("密码是常见弱口令（如 password、123456、finance 之类），请换一个")
    elif any(item in WEAK_TOKENS for item in tokens(raw)):
        hit = next(item for item in tokens(raw) if item in WEAK_TOKENS)
        found.append(f"密码里的「{hit}」是常见弱口令词，请换一个")
    elif any(item in normalized for item in WEAK_SUBSTRINGS):
        hit = next(item for item in WEAK_SUBSTRINGS if item in normalized)
        found.append(f"密码里含有可被猜到的片段「{hit}」，请换一个")

    lowered = raw.lower()
    if any(run in lowered for run in KEYBOARD_RUNS):
        hit = next(run for run in KEYBOARD_RUNS if run in lowered)
        found.append(f"密码里含有键盘/字母顺序片段「{hit[:6]}…」，请换一个")
    if username:
        name = username.strip().lower()
        if name and len(name) >= 4 and name in lowered:
            found.append("密码里不能包含用户名")
    return found
