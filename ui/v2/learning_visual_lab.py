"""Local, keyboard-accessible parameter lab; fixed math and 3D projections."""
import math
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QPushButton
from core.learning.skill.v2.visual import calculate, TEMPLATES


class LabCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = None
        self.azimuth = 35
        self.setMinimumHeight(210)
        self.setAccessibleName('学习实验图形')

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#f5f8fd'))
        p.setPen(QColor('#182941'))
        d = self.data
        if not d:
            return
        if d['curves']:
            groups = [[c] for c in d['curves']] if d['template'] == 'bode' else [d['curves']]
            for i, curves in enumerate(groups):
                box = QRectF(52, 12+i*(self.height()/len(groups)), self.width()-70, self.height()/len(groups)-42)
                self.plot(p, curves, box, d, i)
        elif d['template'] == 'tem':
            self.tem(p, d)
        else:
            self.diagram(p, d['template'])

    def arrow(self, p, a, b, color='#295da8', label=''):
        a, b = QPointF(*a), QPointF(*b)
        p.setPen(QPen(QColor(color), 2))
        p.drawLine(a,b)
        angle = math.atan2(b.y()-a.y(),b.x()-a.x())
        for delta in (-.45,.45):
            p.drawLine(b, QPointF(b.x()-8*math.cos(angle+delta),b.y()-8*math.sin(angle+delta)))
        if label:
            p.drawText(b+QPointF(4,-5),label)

    def plot(self, p, curves, box, data, group):
        xs=[v for c in curves for v in c['x']]; ys=[v for c in curves for v in c['y']]
        xmin,xmax=min(xs),max(xs); ymin,ymax=min(ys),max(ys)
        if data['template']=='step':
            ymin,ymax=0,2
        else:
            pad=max(.1,(ymax-ymin)*.1);ymin-=pad;ymax+=pad
        def point(x,y):
            return QPointF(box.left()+(x-xmin)/max(1e-9,xmax-xmin)*box.width(),box.bottom()-(y-ymin)/max(1e-9,ymax-ymin)*box.height())
        p.setPen(QPen(QColor('#66788c'),1));p.drawRect(box)
        p.drawText(QPointF(box.left(),box.bottom()+16),data['xlabel'])
        p.drawText(QPointF(2,box.top()+15),f'{ymax:.1f}')
        p.drawText(QPointF(2,box.bottom()),f'{ymin:.1f}')
        p.drawText(QPointF(box.left(),box.bottom()-3),f'{xmin:.2g}')
        p.drawText(QPointF(box.right()-35,box.bottom()-3),f'{xmax:.2g}')
        for j,c in enumerate(curves):
            p.setPen(QPen(QColor(['#225fc5','#df6841'][j%2]),2))
            path=QPainterPath();path.moveTo(point(c['x'][0],c['y'][0]))
            for x,y in zip(c['x'][1:],c['y'][1:]):path.lineTo(point(x,y))
            p.drawPath(path);p.drawText(QPointF(box.left()+8,box.top()+16+16*j),c['label'])
        p.setPen(QPen(QColor('#ca2372'),3))
        for x,y in data.get('markers',[]):p.drawEllipse(point(x,y),4,4)

    def tem(self,p,d):
        w,h=self.width(),self.height();a=math.radians(self.azimuth)
        def project(v):
            x,y,z=v
            return (w*.5+85*(x*math.cos(a)-z*math.sin(a)), h*.65-65*(y+.35*(x*math.sin(a)+z*math.cos(a))))
        origin=project((0,0,0))
        for axis,label in [((1.3,0,0),'x'),((0,1.3,0),'y'),((0,0,2.5),'z / k')]:
            self.arrow(p,origin,project(axis),'#697987',label)
        for sample in d['field'][::2]:
            z=sample['z'];e=sample['E'];start=project((0,0,z))
            self.arrow(p,start,project((e[0]*.5,e[1]*.5,z)),'#2289b4')
            self.arrow(p,start,project((-e[1]*.5,e[0]*.5,z)),'#d75541')
        self.arrow(p,origin,project(d['E']),'#2289b4','E')
        self.arrow(p,origin,project([x*376.730313668 for x in d['H']]),'#d75541','H × η')
        p.setPen(QColor('#182941'));p.drawText(10,20,'三维投影：蓝 E · 橙 H · 灰传播轴（箭头分别归一化）')

    def diagram(self,p,kind):
        w,h=self.width(),self.height();y=h*.4
        p.setPen(QColor('#182941'))
        if kind=='boundary':
            p.fillRect(QRectF(30,h*.65,w-60,45),QColor('#abb8c9'))
            for x in [w*.25,w*.5,w*.75]:self.arrow(p,(x,h*.65),(x,h*.22),'#2289b4','E normal')
            p.drawText(QPointF(35,h*.9),'理想导体：E_tangent=0；不是空心波导 TEM 求解')
            return
        labels = ['r','Σ (−)','G','y'] if kind in {'block','signal_flow'} else ['u','B / Σ','∫ dt','x','C → y']
        points=[(35+i*(w-75)/len(labels),y) for i in range(len(labels))]
        for i,((x,y),label) in enumerate(zip(points,labels)):
            p.setPen(QColor('#182941'))
            if kind=='signal_flow':p.drawEllipse(QPointF(x+18,y+15),15,15)
            else:p.drawRoundedRect(QRectF(x,y,55,32),4,4)
            p.drawText(QRectF(x,y,65,32),Qt.AlignmentFlag.AlignCenter,label)
            if i+1<len(points):self.arrow(p,(x+55,y+16),(points[i+1][0],y+16))
        x1=points[-1 if kind!='state_space' else -2][0]+25;x2=points[1][0]+20
        p.setPen(QPen(QColor('#a15696'),2));p.drawLine(QPointF(x1,y+32),QPointF(x1,h*.78));p.drawLine(QPointF(x1,h*.78),QPointF(x2,h*.78))
        self.arrow(p,(x2,h*.78),(x2,y+32),'#a15696','−1' if kind!='state_space' else 'A x')
        if kind=='state_space':p.drawText(12,22,'ẋ=Ax+Bu；y=Cx+Du（D 直通项未画，当前 D=0）')


class VisualLab(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.template='tem';self.phase=0.
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.title=QLabel();self.summary=QLabel();self.summary.setWordWrap(True)
        self.canvas=LabCanvas();layout.addWidget(self.title);layout.addWidget(self.canvas)
        row=QHBoxLayout();self.slider=QSlider(Qt.Orientation.Horizontal);self.slider.setAccessibleName('实验参数')
        self.slider.valueChanged.connect(self.recompute);row.addWidget(self.slider)
        self.play=QPushButton('播放 / 暂停');self.play.clicked.connect(self.toggle);row.addWidget(self.play)
        self.rotate=QPushButton('旋转视角');self.rotate.clicked.connect(self.rotate_view);row.addWidget(self.rotate)
        layout.addLayout(row);layout.addWidget(self.summary)
        self.timer=QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.tick)
        self.set_template('tem')

    def set_template(self,template):
        self.timer.stop();self.template=template;self.phase=0.
        self.title.setText(TEMPLATES[template]);self.slider.blockSignals(True)
        if template=='tem':self.slider.setRange(0,360);self.slider.setValue(0)
        elif template=='root_locus':self.slider.setRange(0,100);self.slider.setValue(10)
        else:self.slider.setRange(10,200);self.slider.setValue(70)
        self.slider.blockSignals(False)
        self.slider.setVisible(template in {'tem','root_locus','step','bode'})
        self.play.setVisible(template=='tem');self.rotate.setVisible(template=='tem')
        self.recompute()

    def recompute(self):
        value=self.slider.value()/(1 if self.template=='tem' else 10 if self.template=='root_locus' else 100)
        self.canvas.data=calculate(self.template,value,phase=self.phase)
        self.summary.setText(self.canvas.data['summary']+'\n'+self.canvas.data['assumptions'])
        self.canvas.update()

    def tick(self):
        self.phase=(self.phase+.15)%(2*math.pi);self.recompute()

    def toggle(self):
        self.timer.stop() if self.timer.isActive() else self.timer.start()

    def rotate_view(self):
        self.canvas.azimuth=(self.canvas.azimuth+25)%360;self.canvas.update()

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
