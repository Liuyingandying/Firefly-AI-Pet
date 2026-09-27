# -*- coding: utf-8 -*-
"""一次性：Stage B —— B站视频解析链迁移到 plugins/firefly_bili_video。

- runner 删除静态导入，改懒加载（缺插件时按能力提示降级）
- 链内 6 个模块 git mv 到插件目录由单独命令完成；本脚本负责代码改写
"""
from pathlib import Path

NL = "\n"
p = Path(__file__).parent / "ui" / "character_conversation_runner.py"
src = p.read_text(encoding="utf-8")


def sub(src, old, new, tag):
    if old not in src:
        raise AssertionError(tag + " 锚点未找到")
    return src.replace(old, new, 1)


# 1) 静态导入 → TYPE_CHECKING + 懒加载助手
old_imports = (
    "from core.video_frame_vision import analyze_video_frame" + NL
    + "from core.video_reader import (" + NL
    + "    SessionVideoContext," + NL
    + "    analyze_video_message," + NL
    + "    detect_bilibili_reference," + NL
    + "    is_video_followup," + NL
    + "    video_reading_failure_reply," + NL
    + ")" + NL
    + "from core.video_time_parser import (" + NL
    + "    format_timestamp," + NL
    + "    parse_video_time_expression," + NL
    + ")" + NL
    + "from core.video_study import (" + NL
    + "    STUDY_ENTRY_REPLY," + NL
    + "    VideoStudyContext," + NL
    + "    handle_study_turn," + NL
    + "    is_quiz_request," + NL
    + "    is_study_entry," + NL
    + "    is_study_exit," + NL
    + "    study_failure_reply," + NL
    + ")"
)
new_imports = (
    "from typing import TYPE_CHECKING" + NL
    + NL
    + "if TYPE_CHECKING:  # 运行时经 _bili_modules() 懒加载（B站视频插件）" + NL
    + "    from core.video_reader import SessionVideoContext  # noqa: F401" + NL
    + "    from core.video_study import VideoStudyContext  # noqa: F401" + NL
    + NL
    + NL
    + "_BILI_REF_RE = re.compile(r\"BV[0-9A-Za-z]{10}|bilibili\\.com\")" + NL
    + NL
    + NL
    + "def _bili_modules():" + NL
    + "    \"\"\"懒加载 B站视频插件实现；未安装时抛 CapabilityMissingError。\"\"\"" + NL
    + "    from core import capabilities" + NL
    + NL
    + "    if not capabilities.is_available(\"bili_video\"):" + NL
    + "        raise capabilities.CapabilityMissingError(" + NL
    + "            \"bili_video\", capabilities.missing_message(\"bili_video\")" + NL
    + "        )" + NL
    + "    import video_frame_vision" + NL
    + "    import video_reader" + NL
    + "    import video_study" + NL
    + "    import video_time_parser" + NL
    + NL
    + "    return video_reader, video_study, video_time_parser, video_frame_vision"
)
src = sub(src, old_imports, new_imports, "imports")

# 2) 链接检测处：能力门控 + 懒加载引用
old_detect = "        video_bvid = detect_bilibili_reference(text)"
new_detect = (
    "        bili_like = bool(_BILI_REF_RE.search(text))" + NL
    + "        if bili_like:" + NL
    + "            from core import capabilities as _cap" + NL
    + NL
    + "            if not _cap.is_available(\"bili_video\"):" + NL
    + "                hint = _cap.missing_message(\"bili_video\")" + NL
    + "                return (" + NL
    + "                    [" + NL
    + "                        AgentEvent.make(" + NL
    + "                            self.AGENT_ID," + NL
    + "                            AgentEventType.FINAL," + NL
    + "                            text=hint," + NL
    + "                        )" + NL
    + "                    ]," + NL
    + "                    hint," + NL
    + "                )" + NL
    + "        vr = vs_mod = vtp_mod = vfv = None" + NL
    + "        if bili_like or self._session_video is not None or self._video_study is not None:" + NL
    + "            try:" + NL
    + "                vr, vs_mod, vtp_mod, vfv = _bili_modules()" + NL
    + "            except _cap.CapabilityMissingError:" + NL
    + "                vr = None  # 插件未装：链接/追问按普通聊天继续" + NL
    + "        video_bvid = vr.detect_bilibili_reference(text) if vr is not None else None"
)
src = sub(src, old_detect, new_detect, "detect gate")

# 3) 阅读分支：懒加载模块前缀
src = sub(src,
    "                result = analyze_video_message(text, on_progress=_emit_video_progress)",
    "                result = vr.analyze_video_message(text, on_progress=_emit_video_progress)",
    "read call")
src = sub(src,
    "                answer = video_reading_failure_reply(exc)",
    "                answer = vr.video_reading_failure_reply(exc)",
    "read failure")
src = sub(src,
    "                self._session_video = SessionVideoContext.from_result(result)",
    "                self._session_video = vr.SessionVideoContext.from_result(result)",
    "read session")
src = sub(src,
    "                    answer += f\"\\n\\n{STUDY_ENTRY_REPLY}\"",
    "                    answer += f\"\\n\\n{vs_mod.STUDY_ENTRY_REPLY}\"",
    "study entry reply")
src = sub(src,
    "                    self._video_study = VideoStudyContext.from_session(",
    "                    self._video_study = vs_mod.VideoStudyContext.from_session(",
    "study ctx")

# 4) 帧追问分支
src = sub(src,
    "            frame_seconds = parse_video_time_expression(",
    "            frame_seconds = vtp_mod.parse_video_time_expression(",
    "frame parse")
src = sub(src,
    "                frame_analysis = analyze_video_frame(",
    "                frame_analysis = vfv.analyze_video_frame(",
    "frame analyze")
src = sub(src,
    "                framed_question = (",
    "                framed_question = (",
    "frame noop")
src = sub(src,
    "                    f\"（{video_reading_failure_reply(exc)}）\"",
    "                    f\"（{vr.video_reading_failure_reply(exc)}）\"",
    "frame failure")

# 5) 学习回合分支
src = sub(src,
    "                 or is_study_exit(text) or is_quiz_request(text) or is_study_entry(text))",
    "                 or vs_mod.is_study_exit(text) or vs_mod.is_quiz_request(text) or vs_mod.is_study_entry(text))",
    "study intercepts a")
src = sub(src,
    "            and is_study_entry(text)" + NL
    + "            and self._video_analysis_enabled()",
    "            and vs_mod.is_study_entry(text)" + NL
    + "            and self._video_analysis_enabled()",
    "study intercepts b")
src = sub(src,
    "                answer, self._video_study = handle_study_turn(",
    "                answer, self._video_study = vs_mod.handle_study_turn(",
    "study turn")
src = sub(src,
    "                answer = study_failure_reply(exc)",
    "                answer = vs_mod.study_failure_reply(exc)",
    "study failure")

# 6) 会话内追问
src = sub(src,
    "                and is_video_followup(text)",
    "                and vs_mod.is_video_followup(text)",
    "followup")

p.write_text(src, encoding="utf-8")
print("runner migrated")
