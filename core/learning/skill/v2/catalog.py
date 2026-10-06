"""Reviewed reasoning nodes, not a new curriculum or generated course database."""
from dataclasses import dataclass, asdict
import hashlib
import json


@dataclass(frozen=True)
class Question:
    text: str
    options: tuple[str, ...]
    correct: int
    reason: str


@dataclass(frozen=True)
class Node:
    id: str
    title: str
    prerequisites: tuple[str, ...]
    claim: str
    explanation: str
    lab: str
    questions: tuple[Question, ...]
    source: str


CONTROL_SOURCE = 'https://ctms.engin.umich.edu/CTMS/?example=Introduction&section=SystemAnalysis'
TEM_SOURCE = 'https://ocw.mit.edu/courses/6-013-electromagnetics-and-applications-spring-2009/'


def q(text, options, answer, reason):
    return Question(text, tuple(options) + ('不知道，先帮我补这一点',), answer, reason)


NODES = {}


def node(id, title, deps, claim, explanation, lab, questions, source=CONTROL_SOURCE):
    NODES[id] = Node(id, title, tuple(deps), claim, explanation, lab, tuple(questions), source)


node('control.error', '参考值与误差', (), '误差来自目标与测量结果的比较。',
     '先看比较点：目标 r 与测量 y 相减得到 e=r−y。误差不是输出，也不是执行器的力。先固定这个信号关系，再讨论如何控制。', 'block', [
     q('目标为 10，测量为 7，按 e=r−y 定义，误差是多少？', ['7', '3', '−3'], 1, '误差是目标减测量：10−7=3。'),
     q('目标保持 10，测量上升到 12，误差如何变化？', ['变成 −2', '仍是 10', '变成 2'], 0, 'e=10−12=−2，测量超过目标后误差改变符号。')])
node('control.loop', '闭环中的返回路径', ('control.error',), '闭环把输出测量返回比较点。',
     '沿图走一圈：误差→控制器→对象→输出测量→比较点。闭环的关键是测量结果会影响下一次输入；有控制器但没有这条返回路径仍可以是开环。', 'signal_flow', [
     q('哪条连接使控制系统形成反馈闭环？', ['目标直接接输出', '测量输出返回比较点', '控制器接电源'], 1, '返回测量使后续控制输入依赖实际输出。'),
     q('移除测量返回路径，但控制器仍在工作，这一定是闭环吗？', ['是，有控制器就行', '是，只要有目标', '不是，输入不再依赖测量输出'], 2, '没有测量返回就不能依靠输出误差调整输入。')])
node('control.feedback', '负反馈的代数关系', ('control.loop',), '负反馈闭环需要解出同时依赖输入与输出的方程。',
     '单位负反馈中 e=r−y，前向通道 y=G e。代入得到 y=G(r−y)，所以 (1+G)y=Gr，闭环比值为 G/(1+G)。这里 G 可以是传递函数；负反馈不保证任意动态系统稳定。', 'block', [
     q('单位负反馈、常数 G=2 时，y/r 是多少？', ['2', '2/3', '−2'], 1, '把 y=2(r−y) 移项得 3y=2r。'),
     q('单位负反馈前向常数 G=4，y/r 是多少？', ['4/5', '5/4', '4'], 0, '闭环分母来自反馈项：y=4(r−y)，5y=4r。')])
node('state.state', '状态保存系统的过去', (), '状态与未来输入一起决定系统未来演化。',
     '质量块的当前位置并不能独自预测下一刻位置：还需要速度。将位置和速度作为状态 x=[位置,速度]，再给出未来外力，就能预测未来运动。状态不是必须等于输出。', 'state_space', [
     q('同位置的两个质量块，一个静止一个在运动，预测未来还需要什么？', ['只需位置', '还需速度', '只需输出名称'], 1, '速度是描述运动历史的必要状态，位置相同不代表未来相同。'),
     q('状态一定与传感器输出完全相同吗？', ['一定', '不一定，输出可能只测部分状态', '状态只指输入'], 1, '输出是状态的测量投影，不能保证包含所有状态。')])
node('state.derivative', '状态方程描述变化率', ('state.state',), 'Ax+Bu 给出状态的变化率，不是状态本身。',
     '连续系统写作 ẋ=Ax+Bu。A 描述当前状态怎样影响变化，B 描述输入怎样影响变化；对变化率积分才得到下一时刻状态。把 ẋ 当 x 会混淆记忆和瞬时作用。', 'state_space', [
     q('ẋ=Ax+Bu 的右侧直接给出什么？', ['状态变化率', '仅输出', '全部历史'], 0, '点号表示时间导数，需积分才能得到状态。'),
     q('要由 ẋ 计算未来的 x，除未来输入外还需要什么？', ['初始状态', '仅 C 矩阵', '不需要其他信息'], 0, '微分方程的解依赖初始条件。')])
node('state.output', '输出是状态与输入的映射', ('state.derivative',), 'y=Cx+Du 描述观察方式，不代替状态演化。',
     '若 x=[位置,速度]，C=[1,0] 且 D=0，输出就是位置。改变 C 可以改变测量的量，但没有改变 A 所描述的系统内部动力学。', 'state_space', [
     q('C=[1,0]、D=0、x=[2,3] 时 y 是多少？', ['3', '5', '2'], 2, 'C 选择第一个状态，即位置 2。'),
     q('仅将 C 从 [1,0] 改成 [0,1]，A 不变，改变了什么？', ['内部动力学一定改变', '输出改为第二个状态', '输入消失'], 1, 'C 决定观测映射；状态动力学仍由 A、B 决定。')])
node('control.damping', '阻尼与超调', ('control.loop',), '标准二阶模型固定自然频率时，阻尼改变超调。',
     '限定零初态、无零点、单位直流增益的标准二阶模型。拖动阻尼 ζ，比较峰值超过稳态值的比例。0<ζ<1 时有振荡；ζ≥1 时这个标准模型无超调。不要推广为任意闭环。', 'step', [
     q('标准二阶模型固定 ωn，ζ 从 0.2 增到 0.7，超调一般怎样？', ['增大', '减小', '不变'], 1, '欠阻尼公式中阻尼增加使指数衰减更强，超调比例减小。'),
     q('固定 ζ 只增大 ωn，标准二阶响应的超调比例怎样？', ['增大', '减小', '保持，主要压缩时间尺度'], 2, '超调比例只由 ζ 决定，ωn 改变时间尺度。')])
node('control.poles', '根轨迹是极点随增益的路径', ('control.feedback',), '根轨迹追踪闭环极点，不是时域响应曲线。',
     '取单位负反馈 G=K/[s(s+1)]，特征方程 s²+s+K=0。拖动 K，看两个根在复平面上的位置。横轴是实部，纵轴是虚部，不是时间与振幅。', 'root_locus', [
     q('根轨迹图上的一个点代表什么？', ['某时刻输出', '给定增益下的闭环极点', '输入频率'], 1, '它是闭环特征方程的根，随增益变化形成轨迹。'),
     q('s²+s+K=0 中 K=1，极点实部为多少？', ['−0.5', '1', '0.5'], 0, '二次公式给出 −1/2 ± j√3/2。')])
node('control.frequency', 'Bode 图的频率变量', ('control.damping',), '频率响应考察不同正弦频率下的增益和相位。',
     '将标准二阶 G(s) 中的 s 替换为 jω。幅频图表示 20log10|G(jω)|，相频图表示相角。横轴 ω 是激励频率 rad/s，不能当成经过时间。', 'bode', [
     q('Bode 图横轴通常是什么？', ['经过时间', '正弦激励角频率', '位置'], 1, '每个点对应一种激励频率的稳态比例与相位。'),
     q('幅值比为 1 时，幅频图为多少 dB？', ['1 dB', '20 dB', '0 dB'], 2, '20log10(1)=0。')])
node('em.transverse', '横向相对于传播轴', (), 'TEM 的 E 和 H 都没有沿传播轴的分量。',
     '先把传播轴固定为 z。TEM 意味着 Ez=Hz=0；E 与 H 可在 xy 平面内取方向。“横向”不等于屏幕水平方向，旋转观察视角不会改变物理定义。', 'tem', [
     q('波沿 z 传播，TEM 的哪两个分量为零？', ['Ex 和 Hy', 'Ez 和 Hz', '全部场分量'], 1, '纵向是传播轴 z，因此两个场的 z 分量均为零。'),
     q('将 TEM 的线极化方向从 x 旋转到 y，仍沿 z 传播，它仍然横向吗？', ['是，仍在 xy 平面', '否，y 是竖直', '只有 x 才横向'], 0, '横向由传播轴定义，x/y 都垂直 z。')], TEM_SOURCE)
node('em.energy', 'E/H/k 的方向关系', ('em.transverse',), '单向无损平面波的 H 方向为 k̂×E。',
     '保持右手坐标系。在 +z 传播的正场相位，E 沿 +x 时 H 沿 +y；E 转到 +y 时 H 转到 −x。E×H 给出能流方向。拖动极化角观察三矢量，而不是把波线当粒子轨迹。', 'tem', [
     q('+z 传播、瞬时 E 沿 +y，H 沿哪个方向？', ['+x', '−x', '+z'], 1, 'z×y=−x，因此 E×H 仍沿 +z。'),
     q('−z 传播、瞬时 E 沿 +x，H 应沿哪边？', ['+y', '−y', '+x'], 1, '传播方向翻转使 k×E 翻转：−z×x=−y。')], TEM_SOURCE)
node('em.boundary', 'TEM 与导波边界', ('em.energy',), '传播模式还要满足导体边界，不能只看两根垂直箭头。',
     '理想导体表面的切向电场为零。双导体传输线允许横截面电势差形成 TEM；理想空心单连通金属波导不能支持非平凡 TEM。这里展示平行板的局部场方向，不是任意波导求解。', 'boundary', [
     q('理想导体表面的哪种电场分量为零？', ['切向', '所有法向', '与方向无关都非零'], 0, '理想导体表面切向 E=0，法向可由表面电荷支持。'),
     q('仅见 E 与 H 互相垂直，能断言任意空心金属波导支持 TEM 吗？', ['能', '不能，还必须满足边界条件', '只看颜色'], 1, '传播场必须满足横截面边界条件，空心单连通理想金属波导无非平凡 TEM。')], TEM_SOURCE)

TOPICS = {
    'tem': ('TEM 波', 'em.boundary', ('tem', '横电磁')),
    'state': ('状态空间', 'state.output', ('状态空间', '状态方程')),
    'feedback': ('反馈控制', 'control.feedback', ('反馈控制', '负反馈', '闭环')),
    'step': ('二阶系统', 'control.damping', ('阶跃', '二阶', '阻尼')),
    'root': ('根轨迹', 'control.poles', ('根轨迹',)),
    'bode': ('频率响应', 'control.frequency', ('bode', '频率响应', '伯德')),
}


def detect(text):
    s = text.casefold().replace(' ', '')
    from core.learning.intents import is_teaching_request
    if not is_teaching_request(s):
        return None
    return next((k for k, (_, _, words) in TOPICS.items() if any(w in s for w in words)), None)


def path_to(target):
    result = []
    def visit(id):
        if id in result:
            return
        for dep in NODES[id].prerequisites:
            visit(dep)
        result.append(id)
    visit(target)
    return result


CATALOG_HASH = hashlib.sha256(json.dumps({k: asdict(v) for k, v in NODES.items()}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def curriculum_evidence(topic):
    """Use the pinned existing loader. No ID inference, registration or writes."""
    if topic == 'tem':
        return {'mode': 'topic', 'course_id': None, 'source': TEM_SOURCE, 'refs': []}
    from learning.orchestrator.curriculum import ROOT, configured_draft, load_curriculum
    mapping = ROOT / 'learning/courses/control_theory/experience.json'
    if not mapping.exists():
        return {'mode': 'unconfigured', 'course_id': None, 'source': CONTROL_SOURCE,
                'refs': [], 'review_units': [],
                'note': '未配置正式课程知识地图；本次使用独立受审教学模板，不代表正式课程进度。'}
    config = json.loads(mapping.read_text(encoding='utf-8'))
    curriculum = load_curriculum(configured_draft(config['runtime_course_id']))
    terms = {'state': ('状态空间',), 'feedback': ('闭环', '控制系统的基本'), 'step': ('二阶',), 'root': ('根轨迹',), 'bode': ('频率',)}[topic]
    refs = [{key: getattr(l, key) for key in ('chapter_id', 'unit_id', 'topic_id', 'concept_id', 'lesson_id', 'title')}
            for l in curriculum.lessons if any(t in l.title for t in terms)]
    return {'mode': 'curriculum_preview', 'course_id': curriculum.course_id, 'sha256': curriculum.sha256,
            'refs': refs, 'review_units': list(curriculum.review_units), 'note': '节点讲解为独立受审教学模板；课程引用只作知识地图，未修改课程内容。'}
