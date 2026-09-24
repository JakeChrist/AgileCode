"""Headless per-file diff read model for unified and aligned presentation."""
from __future__ import annotations
import difflib
import hashlib


def file_diff(path: str, before: str, after: str, *, context: int = 5) -> dict:
    old,new=before.splitlines(),after.splitlines()
    rows=[]
    for tag,a,b,c,d in difflib.SequenceMatcher(None,old,new,autojunk=False).get_opcodes():
        count=max(b-a,d-c)
        for i in range(count):
            if tag=="equal" and count>context*2+3 and context<=i<count-context:
                if i==context:
                    rows.append({"old":None,"new":None,"before":f"… {count-context*2} unchanged lines …","after":f"… {count-context*2} unchanged lines …","kind":"fold"})
                continue
            rows.append({"old":a+i+1 if a+i<b else None,"new":c+i+1 if c+i<d else None,
                         "before":old[a+i] if a+i<b else "","after":new[c+i] if c+i<d else "","kind":tag})
    unified="".join(difflib.unified_diff(before.splitlines(keepends=True),after.splitlines(keepends=True),fromfile="base/"+path,tofile="ticket/"+path,n=context))
    return {"path":path,"rows":rows,"unified":unified,"old_hash":hashlib.sha256(before.encode()).hexdigest(),"new_hash":hashlib.sha256(after.encode()).hexdigest(),
            "old_final_newline":before.endswith("\n"),"new_final_newline":after.endswith("\n"),"old_lines":len(old),"new_lines":len(new)}
