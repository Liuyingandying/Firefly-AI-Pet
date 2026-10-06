"""Read-only presentation of the existing Skill checkpoint; no state storage."""
from core.learning.skill.v2.catalog import NODES, TOPICS
from .context_status_card import ContextStatusView

PHASE_LABELS = {
    'PROBE': '了解你的起点', 'PLAN': '确认学习路径', 'TEACH': '理解这一步',
    'QUIZ': '独立检验', 'FEEDBACK': '查看反馈', 'REMEDIATE': '解决卡点',
    'PAUSED': '已暂停', 'COMPLETE': '本次目标已完成',
}
NEXT_ACTIONS = {
    'PROBE': '选择答案并说明理由；不知道也可以',
    'PLAN': '确认路径后开始一个节点', 'TEACH': '观察图形后开始独立检验',
    'QUIZ': '提交答案和推理过程', 'FEEDBACK': '查看反馈后继续',
    'REMEDIATE': '围绕卡点重新理解，再检验',
    'COMPLETE': '可以自由聊天，或提出新的学习目标',
}


def skill_context(state, *, busy=False):
    """Caller supplies the currently bound owner, never a cached shadow state."""
    phase = state['phase']
    return ContextStatusView(
        mode='学习陪伴', activity=f"{TOPICS[state['topic']][0]} · {PHASE_LABELS[phase]}",
        focus=NODES[state['node']].title,
        progress=f"已验证 {len(state['locked'])}/{len(state['plan'])} 个节点（本次目标）",
        next_action='正在处理当前节点，可以暂停' if busy else NEXT_ACTIONS.get(phase, ''),
    )
