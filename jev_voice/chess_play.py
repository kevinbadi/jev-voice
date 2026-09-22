"""Play chess on chess.com through the agent's CDP tab.

The board is read from the DOM (pieces are ``.piece.<color><type>.square-<file><rank>``; the move list
gives SAN), replayed into python-chess for exact legality, and a move is chosen by Stockfish when it is
installed or by a small built-in search otherwise. Moves are played with two real clicks on the board
squares. Turn detection is the move-list parity; our colour is the board's ``flipped`` flag.

Nothing here is model-driven: the position, the legal moves and the clicks are all code.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from typing import Any, Callable

import chess
import chess.engine
from browser_harness.helpers import cdp

READ = r"""(() => {
  const p=document.querySelector('.piece'); let b=p;
  while (b && !(b.tagName.toLowerCase().includes('board') || /\bboard\b/.test(b.className||''))) b=b.parentElement;
  if (!b) return null;
  const r=b.getBoundingClientRect();
  const pieces=[...b.querySelectorAll('.piece')].map(e=>{
    const m=/\b([wb][pnbrqk])\b/.exec(e.className), s=/square-(\d)(\d)/.exec(e.className);
    return m && s ? {piece:m[1], file:+s[1], rank:+s[2]} : null;}).filter(Boolean);
  const ml=document.querySelector('wc-simple-move-list, .move-list, [class*="move-list"]');
  // chess.com renders piece letters as figurine icons; rebuild SAN per move node so 'Nf3' is not read as 'f3'.
  const nodes=ml ? [...ml.querySelectorAll('[data-ply], .node, [class*="node"]')].filter(n=>!n.querySelector('[data-ply], .node')) : [];
  const sans=nodes.map(n=>{const ic=n.querySelector('[class*="icon-font-chess"], [data-figurine]');
    const cls=(ic&&(ic.className+' '+(ic.getAttribute('data-figurine')||'')))||'';
    const letter=/king/i.test(cls)?'K':/queen/i.test(cls)?'Q':/rook/i.test(cls)?'R':/bishop/i.test(cls)?'B':/knight/i.test(cls)?'N':'';
    return letter+(n.innerText||'').replace(/\s+/g,'').trim();}).filter(Boolean);
  const overEl=document.querySelector('[class*="header-title"], [class*="game-over"], .modal');
  const over=!!document.querySelector('.game-over-modal, [class*="game-over"], .game-result, [class*="game-review"]') ||
             /checkmate|game over|you won|you lost|draw by|resigned|time out/i.test((overEl&&overEl.innerText)||'');
  const promo=[...document.querySelectorAll('.promotion-piece, [class*="promotion"] .piece')].filter(e=>e.checkVisibility()).map(e=>{
    const rr=e.getBoundingClientRect(); const m=/\b([wb][nbrq])\b/.exec(e.className); return {piece:m?m[1]:'', x:rr.x+rr.width/2, y:rr.y+rr.height/2};});
  return {rect:{x:r.x,y:r.y,w:r.width,h:r.height}, flipped:b.classList.contains('flipped'), pieces,
          moves:(ml&&ml.innerText||'').replace(/\s+/g,' ').trim(), sans, over, promo, url:location.href};
})()"""

PIECE_VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def _evaluate(session: str, expression: str) -> Any:
    r = cdp("Runtime.evaluate", session_id=session, expression=expression, returnByValue=True)
    if r.get("exceptionDetails"):
        raise RuntimeError("Page changed while reading the board")
    return r.get("result", {}).get("value")


def read(session: str) -> dict[str, Any] | None:
    return _evaluate(session, READ)


def parse_moves(text: str) -> list[str]:
    """'1. e4 e5 2. Nf3 Nc6' → ['e4', 'e5', 'Nf3', 'Nc6'] (chess.com also renders piece glyphs; keep SAN tokens)."""
    tokens = []
    for tok in re.split(r"\s+", text):
        tok = tok.strip()
        if not tok or re.fullmatch(r"\d+\.+", tok) or tok in {"1-0", "0-1", "1/2-1/2", "*"}:
            continue
        tok = re.sub(r"[^\w=+#\-]", "", tok)
        if re.fullmatch(r"(O-O(-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=[QRBN])?)[+#]?", tok):
            tokens.append(tok)
    return tokens


def moves_of(snapshot: dict[str, Any]) -> list[str]:
    """SAN list: from the move nodes (figurines resolved) when available, else parsed from the text."""
    sans = [t for t in (snapshot.get("sans") or []) if re.fullmatch(r"(O-O(-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=[QRBN])?)[+#]?", t)]
    return sans or parse_moves(snapshot.get("moves", ""))


def position(snapshot: dict[str, Any]) -> chess.Board:
    """Exact position by replaying the move list; falls back to piece placement if the list is unusable."""
    board = chess.Board()
    sans = moves_of(snapshot)
    try:
        for san in sans:
            board.push_san(san)
        placed = {(p["file"], p["rank"]) for p in snapshot["pieces"]}
        if placed and len(placed) != len(board.piece_map()):
            raise ValueError("move list and board disagree")
        return board
    except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        pass
    board = chess.Board(None)
    for p in snapshot["pieces"]:
        color = chess.WHITE if p["piece"][0] == "w" else chess.BLACK
        ptype = {"p": chess.PAWN, "n": chess.KNIGHT, "b": chess.BISHOP, "r": chess.ROOK, "q": chess.QUEEN, "k": chess.KING}[p["piece"][1]]
        board.set_piece_at(chess.square(p["file"] - 1, p["rank"] - 1), chess.Piece(ptype, color))
    board.turn = chess.WHITE if len(sans) % 2 == 0 else chess.BLACK
    rights = ""
    if board.piece_at(chess.E1) == chess.Piece(chess.KING, chess.WHITE):
        rights += "K" if board.piece_at(chess.H1) == chess.Piece(chess.ROOK, chess.WHITE) else ""
        rights += "Q" if board.piece_at(chess.A1) == chess.Piece(chess.ROOK, chess.WHITE) else ""
    if board.piece_at(chess.E8) == chess.Piece(chess.KING, chess.BLACK):
        rights += "k" if board.piece_at(chess.H8) == chess.Piece(chess.ROOK, chess.BLACK) else ""
        rights += "q" if board.piece_at(chess.A8) == chess.Piece(chess.ROOK, chess.BLACK) else ""
    board.set_castling_fen(rights or "-")
    return board


def _material(board: chess.Board) -> int:
    score = 0
    for piece_type, value in PIECE_VALUES.items():
        score += value * (len(board.pieces(piece_type, chess.WHITE)) - len(board.pieces(piece_type, chess.BLACK)))
    score += 5 * (len(list(board.legal_moves)) if board.turn == chess.WHITE else -len(list(board.legal_moves)))
    return score if board.turn == chess.WHITE else -score


def _search(board: chess.Board, depth: int, alpha: int, beta: int) -> int:
    if board.is_checkmate():
        return -100000
    if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_draw():
        return 0
    if depth == 0:
        return _material(board)
    best = -1_000_000
    moves = sorted(board.legal_moves, key=lambda m: (board.is_capture(m), board.gives_check(m)), reverse=True)
    for move in moves:
        board.push(move)
        score = -_search(board, depth - 1, -beta, -alpha)
        board.pop()
        if score > best:
            best = score
        alpha = max(alpha, score)
        if alpha >= beta:
            break
    return best


def choose_move(board: chess.Board, think_s: float = 0.6) -> tuple[chess.Move, str]:
    path = os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish") or "/opt/homebrew/bin/stockfish"
    if os.path.exists(path):
        try:
            with chess.engine.SimpleEngine.popen_uci(path) as engine:
                result = engine.play(board, chess.engine.Limit(time=think_s))
                if result.move:
                    return result.move, "stockfish"
        except (chess.engine.EngineError, OSError):
            pass
    best, best_score = None, -10_000_000
    for move in board.legal_moves:
        board.push(move)
        score = -_search(board, 2, -1_000_000, 1_000_000)
        board.pop()
        if score > best_score:
            best, best_score = move, score
    assert best is not None
    return best, "built-in search"


def square_center(snapshot: dict[str, Any], square: int) -> tuple[float, float]:
    r = snapshot["rect"]
    size = r["w"] / 8
    f, rk = chess.square_file(square), chess.square_rank(square)
    if snapshot["flipped"]:
        f, rk = 7 - f, 7 - rk
    return r["x"] + size * (f + 0.5), r["y"] + size * (7 - rk + 0.5)


def _click(session: str, x: float, y: float) -> None:
    cdp("Input.dispatchMouseEvent", session_id=session, type="mouseMoved", x=x, y=y)
    for event in ("mousePressed", "mouseReleased"):
        cdp("Input.dispatchMouseEvent", session_id=session, type=event, x=x, y=y, button="left", clickCount=1)


def piece_at(snapshot: dict[str, Any], square: int) -> str | None:
    f, rk = chess.square_file(square) + 1, chess.square_rank(square) + 1
    return next((p["piece"] for p in snapshot["pieces"] if p["file"] == f and p["rank"] == rk), None)


def play_move(session: str, snapshot: dict[str, Any], move: chess.Move) -> bool:
    """Click the piece, then click the destination square. Returns True once the board shows the piece there."""
    x0, y0 = square_center(snapshot, move.from_square)
    x1, y1 = square_center(snapshot, move.to_square)
    moving = piece_at(snapshot, move.from_square)
    _click(session, x0, y0)
    time.sleep(0.45)
    _click(session, x1, y1)
    for _ in range(10):
        time.sleep(0.2)
        after = read(session) or {}
        if after.get("pieces") and piece_at(after, move.to_square) == moving and piece_at(after, move.from_square) is None:
            break
    else:
        return False
    if move.promotion:
        time.sleep(0.6)
        after = read(session) or {}
        want = {chess.QUEEN: "q", chess.ROOK: "r", chess.BISHOP: "b", chess.KNIGHT: "n"}[move.promotion]
        for p in after.get("promo", []):
            if p["piece"].endswith(want):
                _click(session, p["x"], p["y"])
                break
    return True


def our_color(snapshot: dict[str, Any]) -> chess.Color:
    return chess.BLACK if snapshot["flipped"] else chess.WHITE


def play(browser: Any, max_moves: int = 200, on_step: Callable[[dict[str, Any]], None] | None = None,
         stop: Callable[[], bool] | None = None, think_s: float = 0.6) -> dict[str, Any]:
    """Loop: wait for our turn, choose, click, wait for the opponent. Returns a summary."""
    session = browser.session
    played: list[dict[str, Any]] = []
    last_len = -1
    idle = 0.0
    while len(played) < max_moves:
        if stop and stop():
            break
        snap = read(session)
        if not snap or not snap["pieces"]:
            time.sleep(0.5)
            idle += 0.5
            if idle > 20:
                return {"result": "no board", "moves": played}
            continue
        board = position(snap)
        if snap["over"] or board.is_game_over():
            return {"result": board.result(claim_draw=True) if board.is_game_over() else "game over", "moves": played,
                    "final": board.fen()}
        if board.turn != our_color(snap):
            time.sleep(0.4)
            idle += 0.4
            if idle > 120:
                return {"result": "opponent idle", "moves": played}
            continue
        idle = 0.0
        sans = moves_of(snap)
        if len(sans) == last_len:
            time.sleep(0.4)  # our click has not registered yet; do not double-move
            continue
        move, engine = choose_move(board, think_s)
        san = board.san(move)
        if not play_move(session, snap, move):
            print(f"  ♟ {san} did not register on the board; re-reading", flush=True)
            time.sleep(0.8)
            continue
        played.append({"ply": len(sans) + 1, "san": san, "engine": engine, "fen": board.fen()})
        last_len = len(sans) + 1
        if on_step:
            on_step({"action": {"label": f"{san} ({engine})", "kind": "chess", "key": None}, "operation": "MOVE", "text": None,
                     "step": len(played)})
        print(f"  ♟ {len(played)}. {san}  [{engine}]", flush=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:  # wait until the board shows our move
            time.sleep(0.3)
            again = read(session)
            if again and len(moves_of(again)) >= last_len:
                break
    return {"result": "move limit", "moves": played}
