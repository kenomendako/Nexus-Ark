import platform
import subprocess
import os
import datetime
import base64
import logging
from typing import Optional, Tuple
from PIL import Image

import constants
import utils

logger = logging.getLogger(__name__)

def _check_wsl() -> bool:
    """現在の環境が WSL (Windows Subsystem for Linux) か判定する"""
    try:
        with open('/proc/version', 'r') as f:
            if 'microsoft' in f.read().lower():
                return True
    except Exception:
        pass
    return False

def _get_roblox_window_rect_windows() -> Optional[Tuple[int, int, int, int]]:
    """
    Windows 環境 (ctypes) で 'Roblox' を含むウィンドウの矩形を取得する。
    戻り値: (left, top, right, bottom) または見つからない場合は None
    """
    try:
        import ctypes
        from ctypes import wintypes
        
        user32 = ctypes.windll.user32
        
        # ウィンドウ列挙用のコールバック型
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        
        found_rect = None
        
        def enum_windows_callback(hwnd, lParam):
            nonlocal found_rect
            
            # ウィンドウが見える状態か確認
            if not user32.IsWindowVisible(hwnd):
                return True
                
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value
                
                if "roblox" in title.lower():
                    class RECT(ctypes.Structure):
                        _fields_ = [
                            ("left", ctypes.c_long),
                            ("top", ctypes.c_long),
                            ("right", ctypes.c_long),
                            ("bottom", ctypes.c_long)
                        ]
                    rect = RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        # 最小化されていない、かつ幅と高さがある程度あるもの
                        if rect.right > rect.left and rect.bottom > rect.top:
                            found_rect = (rect.left, rect.top, rect.right, rect.bottom)
                            return False # 検索停止
            return True
        
        user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)
        return found_rect
        
    except Exception as e:
        logger.error(f"Failed to find Roblox window via ctypes: {e}")
        return None

def _capture_windows_mss() -> Tuple[Optional[Image.Image], bool]:
    """Windowsネイティブ環境で mss を使用してキャプチャする。ウィンドウ検索含む。
    Returns: (画像, Robloxウィンドウを特定できたか)
    """
    import mss
    rect = _get_roblox_window_rect_windows()
    
    with mss.mss() as sct:
        if rect:
            # ウィンドウ矩形が取れた場合
            left, top, right, bottom = rect
            monitor = {"top": top, "left": left, "width": right - left, "height": bottom - top}
            logger.info(f"Capturing Roblox window at: {monitor}")
            sct_img = sct.grab(monitor)
        else:
            # 見つからない場合はメインモニター全体
            logger.info("Roblox window not found. Capturing primary monitor.")
            monitor = sct.monitors[1]  # 0 is all monitors combined, 1 is primary
            sct_img = sct.grab(monitor)
            
        return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"), rect is not None

def _capture_wsl_powershell(save_path: str) -> bool:
    """WSL2 環境から PowerShell を経由してキャプチャし、指定パス(WSL側パス)に保存する。"""
    try:
        # WSLパスをWindowsパスに事前変換
        win_path_result = subprocess.run(
            ["wslpath", "-w", save_path],
            capture_output=True, text=True, timeout=5
        )
        if win_path_result.returncode != 0:
            logger.error(f"wslpath failed: {win_path_result.stderr}")
            return False
        win_save_path = win_path_result.stdout.strip()
        
        # PowerShellスクリプト: 画面全体をキャプチャしてファイル保存
        ps_script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bitmap = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        
        $graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
        
        $bitmap.Save('{win_save_path}', [System.Drawing.Imaging.ImageFormat]::Png)
        
        $graphics.Dispose()
        $bitmap.Dispose()
        """
        
        # PowerShell を実行
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0 and os.path.exists(save_path):
            return True
        else:
            logger.error(f"PowerShell capture failed: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Error executing powershell capture: {e}")
        return False

def capture_roblox_screenshot_impl(room_name: str) -> str:
    """
    スクリーンショット取得のメインロジック。
    環境に応じてキャプチャし、画像を保存・BASE64エンコードする。
    """
    logger.info("Starting screenshot capture...")
    
    # 画像の保存先準備
    # 例: characters/room_name/images/roblox_screenshots/
    room_dir = os.path.join(constants.ROOMS_DIR, room_name)
    save_dir = os.path.join(room_dir, "images", "roblox_screenshots")
    os.makedirs(save_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"roblox_screen_{timestamp}.png"
    save_path = os.path.join(save_dir, filename)
    
    is_wsl = _check_wsl()
    is_windows = platform.system() == "Windows"
    
    img = None
    capture_method = "unknown"
    
    try:
        import mss as mss_module  # 遅延インポート: 未インストールでもアプリは起動可能
    except ImportError:
        return ("エラー: スクリーンショット機能に必要なパッケージ 'mss' がインストールされていません。\n"
                "ターミナルで `pip install mss` を実行してからもう一度お試しください。")
    
    try:
        if is_wsl:
            logger.info("Environment is WSL2. Using PowerShell bridge.")
            capture_method = "WSL_PowerShell"
            if _capture_wsl_powershell(save_path):
                img = Image.open(save_path)
            else:
                return "エラー: WSL環境からのスクリーンキャプチャに失敗しました。"
                
        elif is_windows:
            logger.info("Environment is native Windows. Using mss + ctypes.")
            img, window_found = _capture_windows_mss()
            capture_method = "Windows_Robloxウィンドウ指定" if window_found else "Windows_画面全体（Robloxウィンドウ未検出）"
            if img:
                img.save(save_path)
            else:
                return "エラー: Windows環境でのスクリーンキャプチャに失敗しました。"
                
        else:
            logger.info(f"Environment is {platform.system()}. Using mss fallback (Full screen).")
            capture_method = f"{platform.system()}_MSS"
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                sct_img = sct.grab(monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                img.save(save_path)
                
        if not img:
             return "エラー: キャプチャ画像の生成に失敗しました。"
             
        # 画像リサイズ (API送信用に1024pxに制限)
        resize_result = utils.resize_image_for_api(save_path, max_size=1024, return_image=False)
        if resize_result:
            encoded_string, output_format = resize_result
            mime_type = f"image/{output_format}"
        else:
            # フォールバック
            with open(save_path, "rb") as f:
                encoded_string = base64.b64encode(f.read()).decode("utf-8")
                mime_type = "image/png"
        
        # エージェントへの注入用メッセージ
        # [VIEW_IMAGE: /path/to/image.png] 形式でヒストリーに埋め込む
        note = ""
        if "未検出" in capture_method or "全体" in capture_method:
            note = "\n注意: Robloxウィンドウを特定できなかったため、画面全体をキャプチャしています。Robloxの画面部分を探して判断してください。"
        message = (
            f"スクリーンショットの取得に成功しました (Method: {capture_method})。{note}\n"
            f"[VIEW_IMAGE: {save_path}]\n"
            f"画像はこのメッセージの直後（または記憶システム）に視覚情報として追加されます。\n"
            f"空間認識（オブジェクト座標データ）と見比べて、状況を判断してください。"
        )
        return message

    except Exception as e:
        logger.error(f"Screenshot capture failed: {e}", exc_info=True)
        return f"エラー: スクリーンショット取得中に予期せぬ例外が発生しました: {e}"

from langchain_core.tools import tool

@tool
def capture_roblox_screenshot(room_name: str = "") -> str:
    """
    ROBLOX空間の現在の画面をキャプチャして視覚情報を取得します。
    建築結果の確認、周囲の状況把握、プレイヤーとの位置関係の視覚的確認に使用します。

    Args:
        room_name: (システムで自動入力)
        
    Returns:
        キャプチャ結果の説明と画像参照タグ（画像自体はAIのコンテキストに自動注入されます）
    """
    if not room_name:
        return "エラー: システム内部エラー。room_nameが指定されていません。"
        
    return capture_roblox_screenshot_impl(room_name)

