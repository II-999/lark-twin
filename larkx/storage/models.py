from datetime import datetime
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base
Base = declarative_base()


class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True, autoincrement=True)
    msg_id = Column(String(32), nullable=False)
    chat_id = Column(String(32), nullable=False, index=True)
    chat_name = Column(String(256), default='')
    chat_type = Column(Integer, default=0)
    scope = Column(String(8), default='chat')
    anchor = Column(String(32), default='', index=True)
    root_id = Column(String(32), default='')
    parent_id = Column(String(32), default='')
    sender_id = Column(String(32), default='')
    sender_name = Column(String(256), default='')
    msg_type = Column(Integer, default=0)
    msg_type_name = Column(String(32), default='')
    content = Column(Text, default='')
    content_data = Column(Text, default='')
    position = Column(Integer, default=0)
    create_time = Column(BigInteger, default=0)
    direction = Column(String(4), default='in')
    stored_at = Column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint('msg_id', 'chat_id', name='uq_msg_chat'), Index('ix_chat_anchor_time', 'chat_id', 'anchor', 'create_time'))


class Chat(Base):
    __tablename__ = 'chats'
    chat_id = Column(String(32), primary_key=True)
    name = Column(String(256), default='')
    chat_type = Column(Integer, default=0)
    last_position = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class CatchupMark(Base):
    """补收核对到的 position: 这之前服务端的消息都确认拉过、入过库了。只由补收推进, WS 推来的消息不动它,
    所以中间没推的消息下一轮还补得到(chats.last_position 会被任何一条新入库的消息抬到最新, 不能拿来当核对位置)。"""
    __tablename__ = 'catchup_marks'
    chat_id = Column(String(32), primary_key=True)
    position = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AgentSession(Base):
    __tablename__ = 'agent_sessions'

    session_key = Column(String(80), primary_key=True)
    chat_id = Column(String(32), default='', index=True)
    anchor = Column(String(32), default='')
    agent_session_id = Column(String(64), default='')
    started = Column(Integer, default=0)
    msg_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    last_active = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Action(Base):
    """审计: 通过 larkx 对外做的每个动作(发消息等), 记录是谁、在哪一轮里做的。"""
    __tablename__ = 'actions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    actor = Column(String(16), default='')
    action = Column(String(32), default='')
    chat_id = Column(String(32), default='')
    msg_id = Column(String(32), default='')
    content = Column(Text, default='')
    ok = Column(Integer, default=1)
    error = Column(Text, default='')
    turn_kind = Column(String(16), default='')
    turn_ref = Column(String(64), default='')


class Task(Base):
    """定时任务和终端指令: 到点后由 daemon 交给数字人处理。"""
    __tablename__ = 'tasks'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now)
    kind = Column(String(16), default='task')
    due_at = Column(BigInteger, default=0, index=True)
    every_sec = Column(Integer, default=0)
    prompt = Column(Text, default='')
    chat_id = Column(String(32), default='')
    anchor = Column(String(32), default='')
    by_owner = Column(Integer, default=0)
    creator = Column(String(16), default='')
    status = Column(String(16), default='pending', index=True)
    run_count = Column(Integer, default=0)
    last_run_at = Column(BigInteger, default=0)
    result = Column(Text, default='')


class InboxItem(Base):
    """已收到、还没处理完的触发消息。daemon 重启后据此接着处理, 不丢消息。"""
    __tablename__ = 'inbox'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now)
    msg_id = Column(String(32), default='', index=True)
    chat_id = Column(String(32), default='')
    payload = Column(Text, default='')
    status = Column(String(16), default='pending', index=True)


class Takeover(Base):
    """接管中的会话: 截止前对方的每条消息都直接交给数字人, 不需要触发前缀。"""
    __tablename__ = 'takeovers'
    chat_id = Column(String(32), primary_key=True)
    created_at = Column(DateTime, default=datetime.now)
    until = Column(BigInteger, default=0)
    goal = Column(Text, default='')
    creator = Column(String(16), default='')
    active = Column(Integer, default=1)


class Control(Base):
    """网页等外部入口给 daemon 的控制请求(打断、重置会话), daemon 每秒取一次执行。"""
    __tablename__ = 'controls'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now)
    action = Column(String(32), default='')
    status = Column(String(16), default='pending', index=True)
    result = Column(Text, default='')



class JobRun(Base):
    """例行任务(每日回顾、简报、清理……)的每次运行: 先记下再执行, 状态和结果都看得见。"""
    __tablename__ = 'job_runs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    job = Column(String(32), default='', index=True)
    slot = Column(String(32), default='')          # 这次属于哪个时段: '2026-11-03' | 'once' | 'manual-<时间戳>'
    attempt = Column(Integer, default=1)
    trigger = Column(String(16), default='schedule')  # schedule | manual | startup
    status = Column(String(16), default='queued', index=True)  # queued running ok failed skipped interrupted
    ref = Column(String(64), default='')           # 轮次 ref job:<name>#<id>, 和 actions.turn_ref 对得上
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    summary = Column(Text, default='')
    error = Column(Text, default='')
    __table_args__ = (Index('ix_job_slot', 'job', 'slot'),)


class JobState(Base):
    """例行任务的断点和完成标记(回填进度、初始化记忆的批次)。"""
    __tablename__ = 'job_state'
    job = Column(String(32), primary_key=True)
    data = Column(Text, default='{}')
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class WhitelistEntry(Base):
    """唤起白名单: 除本人外能叫起数字人的人。只按 user_id 匹配; 名字、部门、头像是添加时的快照, 只用来显示。"""
    __tablename__ = 'whitelist'
    user_id = Column(String(32), primary_key=True)
    name = Column(String(256), default='')
    department = Column(String(512), default='')
    avatar_key = Column(String(256), default='')
    added_by = Column(String(16), default='')
    added_at = Column(DateTime, default=datetime.now)


class Setting(Base):
    """网页控制台上改的设置(值是 JSON)。daemon 每次用到时现查, 改了立即生效; 只有本人能改, 数字人改不了。"""
    __tablename__ = 'settings'
    key = Column(String(64), primary_key=True)
    value = Column(Text, default='')
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class EventRule(Base):
    """Owner-authored subscription; executions use the existing Task model."""
    __tablename__ = 'event_rules'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), nullable=False)
    chat_id = Column(String(32), nullable=False)
    instruction = Column(Text, nullable=False)
    sender_ids = Column(Text, default='[]')
    mention_ids = Column(Text, default='[]')
    keywords = Column(Text, default='[]')
    enabled = Column(Integer, default=0)  # -1 deleted, 0 draft/paused, 1 enabled
    since = Column(BigInteger, default=0)
    cursor = Column(Integer, default=0)


class EventTrigger(Base):
    """Dedup and source/task association, not a separate task lifecycle."""
    __tablename__ = 'event_triggers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, nullable=False, index=True)
    chat_id = Column(String(32), nullable=False)
    msg_id = Column(String(32), nullable=False)
    task_id = Column(Integer, nullable=False, unique=True)
    started = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint('rule_id', 'chat_id', 'msg_id', name='uq_event_source'),)
