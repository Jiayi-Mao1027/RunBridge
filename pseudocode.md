function before_prompt_build(original_system_prompt, workspace):
    safety_md = read_if_exists(workspace + "/SAFETY.md")
    permission_md = read_if_exists(workspace + "/PERMISSION.md")

    injected_context = ""
    if safety_md exists:
        injected_context += "\n[SAFETY RULES]\n" + safety_md

    if permission_md exists:
        injected_context += "\n[PERMISSION RULES]\n" + permission_md

    new_system_prompt = injected_context + "\n" + original_system_prompt
    return new_system_prompt