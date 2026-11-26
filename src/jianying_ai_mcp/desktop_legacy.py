"""Windows Jianying <= 6 worker. Run with the pinned desktop Python environment."""
import sys


if __name__ == "__main__":
    from pyJianYingDraft.jianying_controller import ExportFramerate, ExportResolution, JianyingController

    draft_name, output, resolution, rate, timeout = sys.argv[1:]
    controller = JianyingController()
    try:
        # This SDK closes the edited project when switching to the home page.
        if controller.app_status != "home":
            raise RuntimeError("旧版剪映当前处于编辑或导出界面，自动打开目标草稿需要从首页开始")
        controller.export_draft(draft_name, output, resolution=ExportResolution(resolution),
                                framerate=ExportFramerate(f"{rate}fps"), timeout=float(timeout))
    finally:
        if controller.app.Exists(0):
            controller.app.SetTopmost(False)
