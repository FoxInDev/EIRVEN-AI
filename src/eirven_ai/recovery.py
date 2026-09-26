from __future__ import annotations

import time
from typing import Any


class RecoveryEngine:
    """Bounded alternate-path recovery; never loops forever."""
    def __init__(self, services: Any):
        self.services=services

    def open_application(self, target: str) -> dict[str,Any]:
        errors=[]
        try:
            skill=self.services.app_skills.open(target) if self.services.app_skills else {}
            if skill.get("ok"): return {"ok":True,"verified":bool(skill.get("verified")),"method":"skill","result":skill}
            errors.append(str(skill.get("error") or "skill failed"))
        except Exception as exc: errors.append(str(exc))
        try:
            result=self.services.tools.execute("launch_application",{"application":target})
            if result.get("ok"):
                return {"ok":True,"verified":self.services.verifier.application_visible(target) if getattr(self.services,"verifier",None) else True,"method":"launcher","result":result}
            errors.append(str(result.get("error") or "launcher failed"))
        except Exception as exc: errors.append(str(exc))
        # If an app is not installed, continue the owner's workflow in the default
        # browser instead of parking on Windows Search. web_fallback already resolves a
        # likely official service and opens it in the user's real browser/profile.
        try:
            web=self.services.applications.web_fallback(target)
            web_ok=bool(web and (web.get("ok",True) if isinstance(web,dict) else True))
            if web_ok:
                return {"ok":True,"verified":False,"method":"web_fallback","result":web,"recovered":True}
            errors.append(str((web or {}).get("error") if isinstance(web,dict) else "web fallback failed"))
        except Exception as exc: errors.append(str(exc))
        try:
            found=self.services.applications.open_windows_search(target)
            return {"ok":False,"verified":False,"method":"windows_search","result":found,"error":"; ".join(errors[-2:])}
        except Exception as exc: errors.append(str(exc))
        return {"ok":False,"verified":False,"error":"; ".join(errors[-3:])}
