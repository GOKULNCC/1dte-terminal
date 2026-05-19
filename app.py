import webview
import threading
from server import API_HOST, API_PORT, Handler
from http.server import ThreadingHTTPServer
import sys

def start_server():
    print(f"Starting API server at http://{API_HOST}:{API_PORT}")
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    server.serve_forever()

if __name__ == '__main__':
    import os, subprocess, sys
    import ctypes
    
    # Ensure only a single instance of the app runs at a time using a Windows Mutex
    mutex_name = "Global\\AntigravityTradingToolMutex"
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        import win32api, win32con
        try:
            win32api.MessageBox(0, "The Trading Tool is already running.", "Instance Already Open", win32con.MB_ICONWARNING)
        except:
            pass
        sys.exit(0)
    
    # sys.executable is 'pythonw.exe' because of Start_Trading_Tool.bat.
    # We must use 'python.exe' for background scripts so they don't crash when calling print().
    python_exe = sys.executable.replace("pythonw.exe", "python.exe")
    
    # Start background workers (scheduler and live tick streamer)
    # Start background workers (scheduler and live tick streamer)
    print("Starting background workers (scheduler.py, ibkr_live.py)...")
    p_sched = subprocess.Popen([python_exe, "scheduler.py"], creationflags=subprocess.CREATE_NO_WINDOW)
    p_live = subprocess.Popen([python_exe, "ibkr_live.py"], creationflags=subprocess.CREATE_NO_WINDOW)

    # Start the local server in a background daemon thread
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    
    # Create the webview native OS window pointing to the local server
    window = webview.create_window(
        'Trading Tool Dashboard', 
        f'http://{API_HOST}:{API_PORT}', 
        width=1400, 
        height=900
    )
    
    # Start the webview application
    webview.start()
    
    # When the window is closed, clean up orphaned background jobs
    print("Shutting down... cleaning up background processes.")
    try:
        # Use taskkill to forcefully kill the processes and ALL their children (/T)
        # We also kill ollama to free up the 21GB of VRAM it holds for the Qwen model.
        subprocess.run(f"taskkill /F /T /PID {p_sched.pid} /PID {p_live.pid} /IM ollama.exe /IM \"ollama app.exe\"", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print(f"Cleanup error: {e}")
    
    sys.exit(0)
