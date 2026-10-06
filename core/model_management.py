"""System model resources and policy, independent of chat/session/Skill state.

SettingsManager remains the compatible persistence adapter. Credentials belong
to ProviderManager; adapters own protocols; trusted Skill callers declare task
and required capabilities to ModelRouter rather than hard-coding a vendor.
"""
from core.model_router import ModelRouter
from core.settings_manager import SettingsManager


POLICIES = (
    ("tju-stable", "稳定优先（默认）"),
    ("auto", "按任务自动分配"),
    ("tju-max", "Max 优先（失败回稳定）"),
)


class ModelManagement:
    def __init__(self, *, settings=None, router=None):
        self.settings = settings if settings is not None else SettingsManager()
        self.router = router if router is not None else ModelRouter(selection=lambda: self.policy)

    @property
    def policy(self):
        return self.settings.ai_model_profile

    def set_policy(self, policy):
        if policy not in dict(POLICIES):
            raise ValueError("unknown system model policy")
        self.settings.set_ai_model_profile(policy)

    def resources(self, providers):
        rows = []
        for p in self.router.profiles.values():
            try:
                configured = providers.get_provider_status(p.provider)["configured"]
            except KeyError:
                configured = False
            rows.append({"id": p.name, "display_name": p.display_name, "provider": p.provider,
                         "model": p.model, "variant": p.variant, "protocol": p.protocol,
                         "declared_capabilities": p.capabilities,
                         "verified_capabilities": p.verified_capabilities,
                         "evidence": p.evidence, "configured": configured,
                         "fallback": p.fallback})
        return rows

    def task_plan(self):
        return [{"task": task, "profile": self.router.resolve(task=task, profile=self.policy).name,
                 "required_capabilities": tuple(self.router.task_capabilities.get(task, ("text",)))}
                for task in self.router.task_routes]
