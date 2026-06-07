# tools/chess_tools.py
# チェス盤を操作するためのツール群

from langchain_core.tools import tool
from game.chess_engine import game_instance

@tool
def read_board_state() -> str:
    """
    チェス盤の現在の状態をFEN形式で取得する。
    盤面を「見る」にはこのツールを使用する。
    """
    fen = game_instance.get_fen()
    outcome = game_instance.get_outcome()
    free_mode = game_instance.is_free_move_mode()
    
    # Mode indicator
    mode_str = "\nMode: フリームーブ（自由配置）" if free_mode else "\nMode: 通常"
    
    # Only show illegal move attempts in normal mode (not in free move mode)
    attempts_str = ""
    if not free_mode:
        illegal_attempts = game_instance.get_illegal_attempts()
        if illegal_attempts:
            attempts_list = [f"- {a['from']}→{a['to']}" for a in illegal_attempts[-3:]]  # Last 3 only, concise
            attempts_str = "\n不正な手: " + ", ".join(attempts_list)
    
    return f"FEN: {fen}\nStatus: {outcome}{mode_str}{attempts_str}"

@tool
def perform_move(move: str) -> str:
    """
    チェス盤上で駒を動かす。
    move: SAN形式（例：\"e4\", \"Nf3\", \"O-O\"）またはUCI形式（例：\"e2e4\"）で指定する。
    """
    try:
        game_instance.make_move(move)
        return f"Move '{move}' executed successfully.\nNew State: {game_instance.get_fen()}"
    except ValueError as e:
        return f"Error: {str(e)}"

@tool
def get_legal_moves() -> str:
    """
    現在のポジションで可能なすべての合法手のリストを取得する。
    手を決めるときや教えるときに便利。
    """
    moves = game_instance.get_legal_moves()
    return f"Legal Moves: {', '.join(moves)}"

@tool
def reset_game() -> str:
    """
    チェス盤を初期位置にリセットする。
    """
    game_instance.reset_board()
    return "Game reset to starting position."
