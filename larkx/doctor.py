"""larkx doctor: 部署前后的一键检查, 每项给出结论和修复办法。"""
import asyncio
import os
import tempfile

from . import service
from .config import load_config

OK, WARN, FAIL = 'ok', 'warn', 'fail'


def _check(name, level, detail, fix=''):
    return {'name': name, 'level': level, 'detail': detail, 'fix': fix}


def check_login():
    from .protocol import LarkAuth
    auth = LarkAuth()
    ok, msg = auth.validate(fetch_profile=True)
    if not ok:
        return _check('飞书登录', FAIL, msg, '运行 larkx auth qr 扫码登录')
    auth.save()
    return _check('飞书登录', OK, f'user_id={auth.user_id} region={auth.region.name}')


def check_config(cfg):
    out = []
    prefix = (cfg.get('triggers') or {}).get('prefix') or ''
    if cfg.get('allow_self') and not prefix:
        out.append(_check('触发规则', FAIL, '允许自己触发但没有触发前缀, 数字人的回复会再次触发自己',
                          '在 .env 设置 LARKX_TRIGGER_PREFIX, 或关闭 LARKX_ALLOW_SELF'))
    elif not prefix:
        out.append(_check('触发规则', WARN, '没有触发前缀, 所有符合规则的消息都会交给数字人', '在 .env 设置 LARKX_TRIGGER_PREFIX=/ai'))
    else:
        out.append(_check('触发规则', OK, f'前缀 {prefix}  允许自己触发={cfg.get("allow_self")}'))
    if cfg.get('agent_fallback_model'):
        out.append(_check('备用模型', OK, cfg['agent_fallback_model']))
    else:
        out.append(_check('备用模型', WARN, '未设置。主模型被安全策略拦截时会切到 Claude Code 默认备用模型, 接入点没有这个模型时会话会一直失败',
                          '在 .env 设置 LARKX_AGENT_FALLBACK_MODEL 为当前接入点可用的模型'))
    return out


def check_agent_cli(cfg):
    from .agent import BACKENDS, resolve_agent_cli
    backend = cfg.get('agent_backend')
    if backend not in BACKENDS:
        return _check('agent 命令', FAIL, f'未知的 LARKX_AGENT_BACKEND={backend}', f'改成 {" / ".join(BACKENDS)} 之一')
    if backend == 'codex':
        from .agent.codex import user_codex_home
        try:
            path = resolve_agent_cli(cfg.get('codex_cli') or 'codex')
        except FileNotFoundError as e:
            return _check('agent 命令', FAIL, str(e), '安装 Codex, 或把 LARKX_CODEX_CLI 改成 codex 的完整路径')
        if not (user_codex_home() / 'auth.json').exists():
            return _check('agent 命令', FAIL, f'codex 还没登录({user_codex_home()}/auth.json 不存在)', '先运行 codex login')
        return _check('agent 命令', OK, f'codex: {path}, 认证沿用 {user_codex_home()}/auth.json')
    if cfg.get('claude_cli'):
        try:
            return _check('agent 命令', OK, f"claude: {resolve_agent_cli(cfg['claude_cli'])}")
        except FileNotFoundError as e:
            return _check('agent 命令', FAIL, str(e), '把 LARKX_CLAUDE_CLI 改成完整路径')
    if os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY'):
        return _check('agent 命令', OK, 'Agent SDK 自带 claude, 认证取 .env / 环境里的 ANTHROPIC_* 变量')
    return _check('agent 命令', WARN, '未设置 LARKX_CLAUDE_CLI, 环境里也没有 ANTHROPIC_* 认证; 交给 systemd 后很可能调不通模型',
                  '在 .env 设置 LARKX_CLAUDE_CLI 为平时启动 Claude Code 的命令(如 claude 或 ccd), 或填 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN')


async def _probe_model(cfg):
    from .agent import create_session, ensure_agent_home
    from .storage import Storage
    with tempfile.TemporaryDirectory(prefix='larkx-doctor-') as tmp:
        c = dict(cfg, agent_home=f'{tmp}/agent', agent_claude_dir=f'{tmp}/claude', agent_codex_dir=f'{tmp}/codex')
        ensure_agent_home(c)
        s = create_session(c, Storage(f'sqlite:///{tmp}/doctor.db'))
        await s.start()
        try:
            return await s.ask('这是连通性检查, 只回复 OK 两个字母。')
        finally:
            await s.close()


def check_model(cfg):
    try:
        r = asyncio.run(_probe_model(cfg))
    except Exception as e:
        return _check('模型调用', FAIL, f'{e!r}'[:300], _model_fix(cfg))
    if 'OK' in r.text.upper():
        return _check('模型调用', OK, f'{r.duration_s:.1f}s')
    return _check('模型调用', FAIL, f'{r.subtype}: {r.text[:200]}', _model_fix(cfg))


def _model_fix(cfg) -> str:
    if cfg.get('agent_backend') == 'codex':
        return ('确认 codex 在终端里能正常对话; 需要代理时在 .env 写 HTTPS_PROXY / HTTP_PROXY(Codex 读大写变量); '
                '账号不支持默认模型时设置 LARKX_AGENT_MODEL')
    return '检查 LARKX_CLAUDE_CLI 指定的命令能否正常启动 Claude Code, 或 ANTHROPIC_* 认证是否可用'


def check_alert_channel(cfg):
    import shlex
    import shutil
    if not cfg.get('alert_cmd'):
        return _check('备用告警通道', WARN, '未设置 LARKX_ALERT_CMD: 飞书登录失效时没有办法通知你',
                      '例: LARKX_ALERT_CMD=lark-cli im +messages-send --as bot --user-id <你的 open_id> --text {text}')
    exe = shlex.split(cfg['alert_cmd'])[0]
    if not shutil.which(exe):
        return _check('备用告警通道', FAIL, f'找不到命令 {exe}', '把 LARKX_ALERT_CMD 里的命令改成完整路径')
    return _check('备用告警通道', OK, exe)


def check_user_skills():
    from .skills import skill_dir
    missing = [t for t in ('claude', 'codex') if not (skill_dir(t) / 'SKILL.md').exists()]
    if not missing:
        return _check('本机 agent 的 larkx skill', OK, '已装进 Claude Code 和 Codex')
    return _check('本机 agent 的 larkx skill', WARN, f'还没装进: {", ".join(missing)}; 装上后可以直接让它们操作飞书、指挥数字人',
                  'larkx skill install')


def check_service():
    if not service.available():
        return [_check('后台服务', WARN, '本机没有 systemd, 需要自己托管 larkx daemon')]
    st = service.status()
    out = []
    if not st['installed']:
        out.append(_check('后台服务', WARN, '未安装', '运行 larkx service install && larkx service start'))
    elif not st['active']:
        out.append(_check('后台服务', WARN, '已安装但未运行', '运行 larkx service start, 失败时看 larkx service logs'))
    else:
        out.append(_check('后台服务', OK, f'运行中 pid={st["main_pid"]} 开机自启={st["enabled"]}'))
    web = service.status('web')
    if not web['installed'] or not web['active']:
        out.append(_check('网页控制台', WARN, '未安装' if not web['installed'] else '已安装但未运行',
                          '运行 larkx service install && larkx service start, 再用 larkx web url 获取登录链接'))
    else:
        out.append(_check('网页控制台', OK, f'运行中 pid={web["main_pid"]}'))
    stray = service.stray_daemons()
    if stray:
        out.append(_check('重复进程', WARN, f'还有不归 systemd 管的 larkx daemon: {stray}, 同一账号会出现两个数字人',
                          f'确认后结束这些进程: kill {" ".join(map(str, stray))}'))
    return out


def check_jobs(cfg, storage=None):
    """例行任务: 最近一次失败或中断、该跑的每日任务超过 36 小时没完成。"""
    from datetime import datetime, timedelta
    from . import jobs
    from .storage import Storage
    store = jobs.JobStore(storage or Storage())
    bad, overdue, ran = [], [], 0
    for job in jobs.JOBS:
        if job.when(cfg) is None:
            continue
        last = store.last(job.name)
        if not last:
            continue
        ran += 1
        if last.status in ('failed', 'interrupted'):
            bad.append(f'{job.title}({jobs.STATUS_NAMES[last.status]})')
        if job.once or job.when(cfg)[2] is not None:
            continue  # 一次性任务和每周任务不按 36 小时判
        recent = datetime.now() - timedelta(hours=36)
        first = store.log(job.name, limit=1000)[-1]
        done = store.last(job.name, status=('ok', 'skipped'))
        if first.created_at < recent and (not done or done.created_at < recent):
            overdue.append(job.title)
    if bad:
        return _check('例行任务', WARN, '最近一次没成功: ' + '、'.join(bad), 'larkx jobs log 看原因; larkx jobs run <名字> 手动重跑')
    if overdue:
        return _check('例行任务', WARN, '超过 36 小时没有完成: ' + '、'.join(overdue), 'larkx jobs 查看; 确认服务在运行')
    return _check('例行任务', OK, f'{ran} 个任务有运行记录, 都正常' if ran else '还没跑过, 到点会自动运行(larkx jobs 查看计划)')


def check_backfill(cfg, storage=None):
    """首次回填和初始化记忆的进度。"""
    from . import jobs
    from .storage import Storage
    if int(cfg.get('backfill_days') or 0) <= 0:
        return _check('历史回填', OK, '已关闭(LARKX_BACKFILL_DAYS=0)')
    store = jobs.JobStore(storage or Storage())
    bf, bs = store.get_state('backfill'), store.get_state('bootstrap')
    if not bf:
        return _check('历史回填', WARN, f'还没做: daemon 启动后会自动回填最近 {cfg["backfill_days"]} 天', 'larkx service status 确认服务在运行')
    progress = f"{bf.get('chats_done', 0)}/{len(bf.get('order') or [])} 个会话, {bf.get('messages', 0)} 条"
    if not bf.get('done'):
        last = store.last('backfill')
        if last and last.status == 'failed' and len([r for r in store.runs('backfill', last.slot)
                                                     if r.status != jobs.PAUSED]) >= jobs.get('backfill').max_attempts:
            return _check('历史回填', WARN, f'停在 {progress}: 今天重试用完({(last.error or "")[:80]})',
                          '明天会从断点接着回填; 想马上继续就运行 larkx backfill')
        return _check('历史回填', OK, f'进行中: {progress}')
    skipped = f"; 跳过了第 {'、'.join(map(str, bs['skipped']))} 批" if bs.get('skipped') else ''
    memo = ('记忆已初始化' + skipped) if bs.get('done') else (
        f"初始化记忆中: {bs.get('next', 0)}/{-(-len(bs.get('chats') or []) // 8) or '?'} 批" if bs else '等待初始化记忆')
    return _check('历史回填', OK, f'已完成: {progress}; {memo}')


def check_briefing(cfg):
    """每日简报: 时间格式对不对, 找不找得到本人私聊。"""
    from . import jobs, notify
    try:
        t = jobs.briefing_time(cfg)
    except ValueError as e:
        return _check('每日简报', FAIL, str(e), '改 .env 里的 LARKX_BRIEFING_AT, 如 09:00; 关掉写 off')
    if not t:
        return _check('每日简报', OK, '已关闭')
    if not (cfg.get('notify_chat') or notify._load().get('owner_chat')):
        return _check('每日简报', WARN, f'每天 {t[0]:02d}:{t[1]:02d} 发, 但还没找到本人私聊',
                      'daemon 第一次发通知时会自动找到; 也可以设 LARKX_NOTIFY_CHAT')
    return _check('每日简报', OK, f'每天 {t[0]:02d}:{t[1]:02d} 发到本人私聊, 周一附上周回顾')


def check_memory(cfg):
    """结构化记忆: 文件数、待跟进、字段问题; 没打开时不检查。"""
    from pathlib import Path
    from . import memory
    home = Path(cfg['agent_home'])
    if not cfg.get('memory_structured'):
        return None
    if not (home / 'memory').is_dir():
        return _check('长期记忆', OK, '还没有(daemon 第一次启动时创建)')
    s = memory.stats(home)
    detail = f"{s['files']} 个文件, 归档 {s['archived']} 个, 待跟进 {s['todo']} 件"
    if s['errors']:
        return _check('长期记忆', WARN, f"{detail}; {s['errors']} 个字段错误", 'larkx memory lint 查看; 每日回顾时分身会自己改')
    return _check('长期记忆', OK, detail + (f"; {s['warnings']} 个提醒(larkx memory lint)" if s['warnings'] else ''))


def run(skip_model=False) -> list:
    cfg = load_config()
    checks = [check_login(), *check_config(cfg), check_agent_cli(cfg)]
    if not skip_model and checks[-1]['level'] != FAIL:
        checks.append(check_model(cfg))
    checks.extend(check_service())
    checks.append(check_alert_channel(cfg))
    checks.append(check_user_skills())
    checks.append(check_jobs(cfg))
    checks.append(check_backfill(cfg))
    checks.append(check_briefing(cfg))
    mem = check_memory(cfg)
    if mem:
        checks.append(mem)
    return checks
