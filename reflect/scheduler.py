import os, json, time as _time, socket as _socket, logging
from datetime import datetime, date, timedelta

def _is_last_workday_of_week(today=None):
    """判断今天是否为本周（周一~周日）最后一个工作日，支持中国调休"""
    try:
        from chinese_calendar import is_workday
    except ImportError:
        # fallback: 没有chinese_calendar时按普通周五判断
        if today is None: today = date.today()
        return today.weekday() == 4
    if today is None: today = date.today()
    if not is_workday(today):
        return False
    # 检查从明天到本周日是否还有工作日
    d = today + timedelta(days=1)
    sunday = today + timedelta(days=(6 - today.weekday()))
    while d <= sunday:
        if is_workday(d):
            return False
        d += timedelta(days=1)
    return True

# 端口锁：防止重复启动，bind失败时agentmain会直接崩溃退出
# reload时mod.__dict__保留_lock，跳过重复绑定
try: _lock
except NameError:
    _lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _lock.bind(('127.0.0.1', 45762)); _lock.listen(1)

INTERVAL = 120
ONCE = False

_dir = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(_dir, '../sche_tasks')
DONE  = os.path.join(_dir, '../sche_tasks/done')
_LOG  = os.path.join(_dir, '../sche_tasks/scheduler.log')

os.makedirs(DONE, exist_ok=True)
_logger = logging.getLogger('scheduler')
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                        datefmt='%Y-%m-%d %H:%M'))
    _logger.addHandler(_fh)

# 默认最大延迟窗口（小时），超过此时间不触发
DEFAULT_MAX_DELAY = 6
_l4_t = 0  # last L4 archive time

# --- 触发防抖：防止单个失败任务阻塞所有后续任务 ---
_pending = {}  # tid -> {'at': datetime, 'attempts': int, 'last_run': datetime|None}
_TRIGGER_COOLDOWN_MIN = 10   # 同一任务触发后 N 分钟内不再重复触发
_MAX_TRIGGER_ATTEMPTS = 3    # 连续触发 N 次仍无 done 文件 → 暂时跳过

def _parse_cooldown(repeat):
    """解析repeat为冷却时间(比实际周期略短,防漂移)"""
    if repeat == 'once': return timedelta(days=999999)
    if repeat in ('daily', 'weekday'): return timedelta(hours=20)
    if repeat == 'weekly': return timedelta(days=6)
    if repeat == 'monthly': return timedelta(days=27)
    if repeat.startswith('every_'):
        try:
            parts = repeat.split('_')
            n = int(parts[1].rstrip('hdm'))
            u = parts[1][-1]
            if u == 'h': return timedelta(hours=n)
            if u == 'm': return timedelta(minutes=n)
            if u == 'd': return timedelta(days=n)
        except (ValueError, IndexError):
            pass  # fall through to warning below
    _logger.warning(f'Unknown repeat type: {repeat}, fallback to 20h cooldown')
    return timedelta(hours=20)

def _last_run(tid, done_files):
    """找最近一次执行时间"""
    latest = None
    for df in done_files:
        if not df.endswith(f'_{tid}.md'): continue
        try:
            t = datetime.strptime(df[:15], '%Y-%m-%d_%H%M')
            if latest is None or t > latest: latest = t
        except: continue
    return latest

def check():
    # L4 archive cron (silent, every 12h)
    global _l4_t
    if _time.time() - _l4_t > 43200:
        _l4_t = _time.time()
        try:
            import sys; sys.path.insert(0, os.path.join(_dir, '../memory/L4_raw_sessions'))
            from compress_session import batch_process
            raw_dir = os.path.join(_dir, '../temp/model_responses')
            r = batch_process(raw_dir, dry_run=False)
            print(f'[L4 cron] {r}')
        except Exception as e:
            _logger.error(f'L4 archive failed: {e}')

    if not os.path.isdir(TASKS): return None
    now = datetime.now()
    os.makedirs(DONE, exist_ok=True)
    done_files = set(os.listdir(DONE))
    for f in sorted(os.listdir(TASKS)):
        if not f.endswith('.json'): continue
        tid = f[:-5]
        try:
            with open(os.path.join(TASKS, f), encoding='utf-8') as fp:
                task = json.loads(fp.read())
        except Exception as e:
            _logger.error(f'JSON parse error for {f}: {e}')
            continue
        if not task.get('enabled', False): continue
        
        repeat = task.get('repeat', 'daily')
        sched = task.get('schedule', '00:00')
        try:
            h, m = map(int, sched.split(':'))
        except Exception as e:
            _logger.error(f'Invalid schedule format in {f}: {sched!r} ({e})')
            continue
        
        # weekday任务：周末跳过
        if repeat == 'weekday' and now.weekday() >= 5: continue
        
        # weekly_last_workday：本周最后一个工作日才触发（支持中国调休）
        if repeat == 'weekly_last_workday':
            if not _is_last_workday_of_week(now.date()):
                continue
        
        # weekly任务：如果指定了day字段，只在对应星期几触发
        if repeat == 'weekly' and 'day' in task:
            _day_map = {'monday':0,'tuesday':1,'wednesday':2,'thursday':3,
                        'friday':4,'saturday':5,'sunday':6}
            target_day = _day_map.get(task['day'].lower())
            if target_day is not None and now.weekday() != target_day:
                continue
        
        # 还没到schedule时间就跳过
        if now.hour < h or (now.hour == h and now.minute < m): continue
        
        # 执行窗口检查：超过max_delay小时则跳过（防止开机太晚触发过时任务）
        max_delay = task.get('max_delay_hours', DEFAULT_MAX_DELAY)
        sched_minutes = h * 60 + m
        now_minutes = now.hour * 60 + now.minute
        if (now_minutes - sched_minutes) > max_delay * 60:
            _logger.info(f'SKIP {tid}: {now_minutes - sched_minutes}min past schedule, '
                         f'exceeds max_delay={max_delay}h')
            continue
        
        # 检查冷却
        last = _last_run(tid, done_files)
        cooldown = _parse_cooldown(repeat)
        # 日历日修正：daily/weekday 任务只要跨天了就不受小时冷却限制
        if last and repeat in ('daily', 'weekday') and last.date() < now.date():
            pass  # 已跨天，允许触发
        elif last and (now - last) < cooldown:
            continue
        
        # --- 触发防抖：防止卡死任务阻塞后续任务 ---
        if tid in _pending:
            p = _pending[tid]
            new_done = (last is not None and last != p['last_run'])
            if new_done:
                # done 文件已出现，任务已完成，清除 pending 状态
                del _pending[tid]
            else:
                # 仍未完成
                elapsed = (now - p['at']).total_seconds() / 60
                if elapsed < _TRIGGER_COOLDOWN_MIN:
                    continue  # 冷却中，跳过，让后面的任务有机会
                if p['attempts'] >= _MAX_TRIGGER_ATTEMPTS:
                    _logger.warning(
                        f'BLOCK {tid}: triggered {p["attempts"]} times '
                        f'without completion since {p["at"]:%H:%M}, skipping')
                    continue  # 超过最大重试，跳过

        # 触发
        _pending[tid] = {'at': now, 'last_run': last,
                         'attempts': _pending.get(tid, {}).get('attempts', 0) + 1}
        _logger.info(f'TRIGGER {tid} (repeat={repeat}, schedule={sched}, '
                     f'last_run={last}, attempt={_pending[tid]["attempts"]})')
        ts = now.strftime('%Y-%m-%d_%H%M')
        rpt = os.path.join(DONE, f'{ts}_{tid}.md')
        prompt = task.get('prompt', '')
        return (f'[定时任务] {tid}\n'
                f'[报告路径] {rpt}\n\n'
                f'先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n'
                f'{prompt}\n\n'
                f'完成后将执行报告写入 {rpt}。')

    return None
