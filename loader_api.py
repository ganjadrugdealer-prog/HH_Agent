class LoaderApi:
    def __init__(self, window):
        self.window = window

    def install_core(self):
        import core_manager
        import threading
        
        def _task():
            try:
                def progress_cb(msg):
                    if self.window:
                        self.window.evaluate_js(f'update_progress("{msg}")')
                
                core_manager.install_core(progress_cb)
                
                if self.window:
                    self.window.evaluate_js('update_progress("Готово! Перезапуск...")')
                import time
                time.sleep(1)
                
                # Restart the app
                restart_app()
            except Exception as e:
                import traceback
                traceback.print_exc()
                if self.window:
                    self.window.evaluate_js(f'document.getElementById("status-text").innerText = "Ошибка: {e}";')
        
        threading.Thread(target=_task, daemon=True).start()
        return {'status': 'started'}
