"""Small reusable renderer for local guided-flow step lists."""


_STATUS_LABELS = {
    "complete": "완료 ✓",
    "current": "현재 단계",
    "waiting": "대기",
    "problem": "문제 해결 필요",
}


def render_guided_stepper(ui, container, steps: list[dict]) -> None:
    """Render ordered guided steps with one visually emphasized current step."""
    container.clear()
    with container:
        for step in steps:
            status = step.get("status", "waiting")
            emphasis = ("border-l-4 border-primary bg-blue-50 font-semibold" if status == "current"
                        else "border-l-4 border-amber-500 bg-amber-50 font-semibold" if status == "problem"
                        else "border-l-4 border-transparent")
            with ui.row().classes(f"w-full items-center justify-between gap-3 rounded px-3 py-2 {emphasis}"):
                ui.label(f"{step.get('number')}. {step.get('label')}").classes("grow")
                ui.label(_STATUS_LABELS.get(status, "대기")).classes(
                    "text-sm text-amber-800" if status == "problem" else "text-sm text-gray-600")
