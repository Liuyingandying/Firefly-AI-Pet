"""Deterministic template calculations. No model-generated code execution."""
import cmath
import math
import numpy as np

TEMPLATES = {
    'block': '负反馈框图', 'signal_flow': '信号流图', 'state_space': '状态空间结构',
    'step': '标准二阶阶跃响应', 'root_locus': 'G=K/[s(s+1)] 根轨迹',
    'bode': '标准二阶 Bode 图', 'tem': 'TEM 三维 E/H/k', 'boundary': '理想导体边界',
}


def finite(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError('INVALID_LAB_PARAMETER')
    return float(value)


def calculate(template, value=0.7, *, omega=2.0, phase=0.0):
    if template not in TEMPLATES:
        raise ValueError('UNSUPPORTED_TEMPLATE')
    phase = finite(phase, 0, 2*math.pi)
    omega = finite(omega, 0.5, 10)
    result = {'template': template, 'version': 1, 'title': TEMPLATES[template], 'curves': [], 'phase': phase}
    if template in {'step', 'bode'}:
        zeta = finite(value, 0.1, 2)
        result['parameters'] = {'zeta': zeta, 'omega_n_rad_s': omega}
        result['assumptions'] = '零初态；无零点；单位直流增益；连续 LTI'
        if template == 'step':
            decay = zeta if zeta <= 1 else zeta-math.sqrt(zeta*zeta-1)
            t = np.linspace(0, 8/(decay*omega), 1001)
            tau = omega*t
            if abs(zeta-1) < 1e-7:
                y = 1-np.exp(-tau)*(1+tau)
            elif zeta < 1:
                d = math.sqrt(1-zeta*zeta)
                y = 1-np.exp(-zeta*tau)*(np.cos(d*tau)+zeta/d*np.sin(d*tau))
            else:
                d = math.sqrt(zeta*zeta-1)
                a, b = -zeta+d, -zeta-d
                y = 1+(b*np.exp(a*tau)-a*np.exp(b*tau))/(a-b)
            result.update(xlabel='t (s)', ylabel='y / unit step',
                          curves=[{'label': 'y(t)', 'x': t.tolist(), 'y': y.tolist()},
                                  {'label': 'steady=1', 'x': t.tolist(), 'y': np.ones_like(t).tolist()}],
                          summary=f'数值超调 {max(0,float(max(y))-1)*100:.2f}% · ζ={zeta:.2f} · ωn={omega:.1f} rad/s')
        else:
            w = np.logspace(-2, 2, 401)*omega
            h = omega**2/((1j*w)**2+2*zeta*omega*(1j*w)+omega**2)
            result.update(xlabel='log10 ω (rad/s)', ylabel='dB / phase shown separately',
                          curves=[{'label': 'magnitude (dB)', 'x': np.log10(w).tolist(), 'y': (20*np.log10(abs(h))).tolist()},
                                  {'label': 'phase (deg)', 'x': np.log10(w).tolist(), 'y': np.rad2deg(np.unwrap(np.angle(h))).tolist()}],
                          summary=f'ζ={zeta:.2f} · ωn={omega:.1f} rad/s · 两图独立纵轴')
    elif template == 'root_locus':
        gain = finite(value, 0, 10)
        gains = np.linspace(0, 10, 501)
        roots = [(-1+np.sqrt(1-4*gains+0j))/2, (-1-np.sqrt(1-4*gains+0j))/2]
        marker = [(-1+cmath.sqrt(1-4*gain))/2, (-1-cmath.sqrt(1-4*gain))/2]
        result.update(parameters={'K': gain}, assumptions='单位负反馈；开环 K/[s(s+1)]',
                      xlabel='Re(s)', ylabel='Im(s)',
                      curves=[{'label': f'pole {i+1}', 'x': r.real.tolist(), 'y': r.imag.tolist()} for i,r in enumerate(roots)],
                      markers=[[r.real,r.imag] for r in marker], summary=f'K={gain:.2f}；s²+s+K=0')
    elif template == 'tem':
        angle = finite(value, 0, 360)*math.pi/180
        eta = 376.730313668
        e = np.array([math.cos(angle), math.sin(angle), 0.0])*math.cos(phase)
        k = np.array([0., 0., 1.])
        h = np.cross(k, e)/eta
        result.update(parameters={'polarization_deg': value, 'phase_rad': phase, 'frequency_hz': 1e9},
                      assumptions='真空单向线极化行波；+z；E/H 箭头分别归一化；动画为慢放',
                      E=e.tolist(), H=h.tolist(), k=k.tolist(), S=np.cross(e,h).tolist(),
                      field=[{'z': float(z), 'E': (np.array([math.cos(angle),math.sin(angle),0])*math.cos(2*math.pi*z-phase)).tolist()}
                             for z in np.linspace(0, 2, 33)],
                      summary=f'θ={value:.0f}° · Ez=Hz=0 · λ=0.299792458 m · E:V/m H:A/m')
        if abs(float(np.dot(e,h))) > 1e-12 or float(np.dot(np.cross(e,h),k)) < -1e-12:
            raise ValueError('PHYSICS_INVARIANT_FAILED')
    else:
        result.update(parameters={}, assumptions='教学结构示意；不代表任意对象数值模型',
                      summary={'block':'e=r−y；y=G e；单位负反馈',
                               'signal_flow':'r → e → u → y；y 经 −1 返回 e',
                               'state_space':'ẋ=Ax+Bu；y=Cx+Du；积分器保存状态',
                               'boundary':'理想导体：切向 E=0；法向 E 可由表面电荷支持'}[template])
    for curve in result['curves']:
        if not all(math.isfinite(x) for x in curve['x']+curve['y']):
            raise ValueError('NONFINITE_RESULT')
    result['validation'] = 'deterministic_checks_passed'
    return result
