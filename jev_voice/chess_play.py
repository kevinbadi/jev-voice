"""Play chess on chess.com through the agent's CDP tab.

The board is read from the DOM (pieces are ``.piece.<color><type>.square-<file><rank>``; the move list
gives SAN), replayed into python-chess for exact legality, and a move is chosen by Stockfish when it is
installed or by a small built-in search otherwise. Moves are played with two real clicks on the board
squares. Turn detection is the move-list parity; our colour is the board's ``flipped`` flag.

Nothing here is model-driven: the position, the legal moves and the clicks are all code.
"""
from __future__ import annotations

import atexit
import os
import random
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
  // A live game (not a lobby preview board): a game URL, or in-game controls such as Resign / Abort / Draw.
  const controls=[...document.querySelectorAll('button,[role="button"]')].filter(e=>e.checkVisibility())
    .map(e=>((e.getAttribute('aria-label')||'')+' '+(e.innerText||'')).toLowerCase());
  const live=/\/game\//.test(location.pathname) || controls.some(t=>/\b(resign|abort|offer draw|draw)\b/.test(t));
  return {live, rect:{x:r.x,y:r.y,w:r.width,h:r.height}, flipped:b.classList.contains('flipped'), pieces,
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


_engine: chess.engine.SimpleEngine | None = None


def _stockfish() -> chess.engine.SimpleEngine | None:
    """One long-lived Stockfish process (spawning per move cost ~0.3 s and could hang)."""
    global _engine
    if _engine is not None:
        return _engine
    path = os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish") or "/opt/homebrew/bin/stockfish"
    if not os.path.exists(path):
        return None
    try:
        _engine = chess.engine.SimpleEngine.popen_uci(path, timeout=10.0)
        atexit.register(close_engine)
    except (chess.engine.EngineError, OSError):
        _engine = None
    return _engine


def close_engine() -> None:
    global _engine
    if _engine is not None:
        try:
            _engine.quit()
        except Exception:  # noqa: BLE001
            pass
        _engine = None


def candidates(board: chess.Board, think_s: float = 0.6, n: int = 5) -> list[dict[str, Any]]:
    """Stockfish's top-n moves with evaluations (centipawns from our side), best first."""
    engine = _stockfish()
    if engine is None:
        return []
    try:
        infos = engine.analyse(board, chess.engine.Limit(time=think_s), multipv=n)
    except (chess.engine.EngineError, OSError, TimeoutError):
        close_engine()
        return []
    out = []
    for i, info in enumerate(infos):
        pv = info.get("pv")
        if not pv:
            continue
        score = info["score"].pov(board.turn)
        cp = score.score(mate_score=100000)
        out.append({"move": pv[0], "san": board.san(pv[0]), "rank": i + 1, "cp": cp,
                    "eval": (f"mate in {score.mate()}" if score.is_mate() else f"{cp / 100:+.2f}"),
                    "line": " ".join(board.variation_san(pv[:4]).split()[:6])})
    return out


def jev_choose(board: chess.Board, options: list[dict[str, Any]], style: str | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Jev selects the move from Stockfish's candidates. The default criterion is strength (rank 1 wins)."""
    from jev_ultrafast.model import post_json, validate_choice

    from . import config

    key = os.environ.get("TYPESAFE_API_KEY") or config.TYPESAFE_API_KEY
    if not key or not options:
        return None
    ids = {f"m{o['rank']}": o for o in options}
    criteria = {k: f"{o['san']} · engine rank #{o['rank']} · eval {o['eval']} · line {o['line']}" for k, o in ids.items()}
    instructions = ("Choose the move to play from the engine's candidates. Default: the strongest move (engine rank #1, best eval). "
                    + (f"Style preference from the user: {style}. Deviate from #1 only when a candidate within 0.3 of the best eval fits "
                       "that style clearly better." if style else "Pick a lower-ranked move only if its eval is equal to the best."))
    body = {"model": os.environ.get("TYPESAFE_MODEL", config.JEV_MODEL),
            "state": {"fen": board.fen(), "to_move": "white" if board.turn else "black", "candidates": criteria},
            "questions": {"move": {"type": "choice", "criteria": criteria, "instructions": instructions}}}
    started = time.time()
    try:
        result = post_json(config.TYPESAFE_URL, key, body)
        answer = validate_choice(result["answers"]["move"], ids)
    except Exception as error:  # noqa: BLE001
        print(f"  ! jev move choice failed ({str(error)[:60]}); playing engine #1", flush=True)
        return None
    return ids[answer["choice"]], {"probabilities": {ids[k]["san"]: v for k, v in answer["probabilities"].items()},
                                   "confidence": answer["confidence"], "latency_ms": round((time.time() - started) * 1000),
                                   "usage": result.get("usage", {})}


LAST_DECISION: dict[str, Any] = {}


def facts_for(board: chess.Board, move: chess.Move, options: list[dict[str, Any]], option: dict[str, Any]) -> dict[str, str]:
    """True statements about the chosen move, computed in code. Jev picks the one worth saying."""
    piece = board.piece_at(move.from_square)
    name = chess.piece_name(piece.piece_type) if piece else "piece"
    out: dict[str, str] = {}
    if board.is_capture(move):
        taken = board.piece_at(move.to_square)
        out["capture"] = f"it takes the {chess.piece_name(taken.piece_type) if taken else 'pawn'} on {chess.square_name(move.to_square)}"
    if board.gives_check(move):
        out["check"] = "it gives check"
    if board.is_castling(move):
        out["castle"] = "it castles, tucking the king away and connecting the rooks"
    if piece and piece.piece_type in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(move.from_square) in (0, 7):
        out["develop"] = f"it develops the {name} toward the centre"
    if piece and piece.piece_type == chess.PAWN and chess.square_file(move.to_square) in (3, 4) and not board.is_capture(move):
        out["centre"] = "it claims space in the centre"
    board.push(move)
    them = board.turn  # after our move it is their turn
    attacked = [chess.square_name(sq) for sq in board.attacks(move.to_square)
                if (pc := board.piece_at(sq)) and pc.color == them and pc.piece_type in (chess.QUEEN, chess.ROOK)]
    board.pop()
    if attacked:
        target = board.piece_at(chess.parse_square(attacked[0]))
        out["threat"] = f"it attacks the {chess.piece_name(target.piece_type) if target else 'piece'} on {attacked[0]}"
    if len(options) > 1:
        gap = option["cp"] - options[1]["cp"] if option["rank"] == 1 else options[0]["cp"] - option["cp"]
        if option["rank"] == 1 and gap >= 80:
            out["clear"] = f"it is clearly best, {gap / 100:.1f} pawns ahead of {options[1]['san']}"
        elif option["rank"] == 1:
            out["close"] = f"{options[1]['san']} was nearly as good"
    if option["cp"] >= 300:
        out["winning"] = "the position is winning"
    elif option["cp"] <= -300:
        out["losing"] = "the position is difficult; this limits the damage"
    return out or {"plan": f"it follows the engine's plan: {option['line']}"}


def teaching_line(board: chess.Board, move: chess.Move, options: list[dict[str, Any]], option: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One spoken sentence: the move, the fact Jev chose to stress, the eval, the runner-up."""
    from jev_ultrafast.model import post_json, validate_choice

    from . import config

    facts = facts_for(board, move, options, option)
    san = board.san(move)
    key = os.environ.get("TYPESAFE_API_KEY") or config.TYPESAFE_API_KEY
    chosen = next(iter(facts))
    meta: dict[str, Any] = {"facts": facts, "chosen_fact": chosen}
    if key and len(facts) > 1:
        try:
            r = post_json(config.TYPESAFE_URL, key, {"model": os.environ.get("TYPESAFE_MODEL", config.JEV_MODEL),
                "state": {"move": san, "eval": option["eval"], "line": option["line"], "candidates": [(o["san"], o["eval"]) for o in options]},
                "questions": {"why": {"type": "choice", "criteria": facts,
                                      "instructions": "Every fact is true of this move. Which one best explains to a learner why it is the move to play?"}}})
            answer = validate_choice(r["answers"]["why"], facts)
            chosen = answer["choice"]
            meta.update(chosen_fact=chosen, probabilities=answer["probabilities"], usage=r.get("usage", {}))
        except Exception:  # noqa: BLE001
            pass
    alt = f" {options[1]['san']} was the alternative at {options[1]['eval']}." if len(options) > 1 and option["rank"] == 1 else ""
    spoken = f"{san}: {facts[chosen]}. Stockfish rates it {option['eval']}.{alt}"
    if os.environ.get("CHESS_EXPLAIN", "llm") == "llm":
        try:
            from . import escalate

            if escalate.enabled():
                text, emeta = escalate.explain_move(board.fen(), san, facts, chosen, option, options)
                if text:
                    spoken = text
                    meta["explained_by"] = emeta.get("model")
        except Exception as error:  # noqa: BLE001
            meta["explain_error"] = str(error)[:80]
    return spoken, meta


def choose_move(board: chess.Board, think_s: float = 0.6, style: str | None = None) -> tuple[chess.Move, str]:
    global LAST_DECISION
    options = candidates(board, think_s)
    if options:
        picked = jev_choose(board, options, style) if os.environ.get("CHESS_CHOOSER", "jev") == "jev" else None
        if picked:
            option, meta = picked
            tag = f"jev {meta['probabilities'].get(option['san'], 0):.0%} · stockfish #{option['rank']} {option['eval']} · {meta['latency_ms']} ms"
        else:
            option, meta = options[0], {"probabilities": {}, "confidence": None, "latency_ms": 0}
            tag = f"stockfish #1 {options[0]['eval']}"
        LAST_DECISION = {
            "fen": board.fen(),
            "question": "Choose the move to play from the engine's candidates. Default: the strongest move (engine rank #1)."
                        + (f" Style: {style}." if style else ""),
            "candidates": [{"san": o["san"], "rank": o["rank"], "eval": o["eval"], "cp": o["cp"], "line": o["line"],
                            "p": meta["probabilities"].get(o["san"])} for o in options],
            "chosen": option["san"], "jev_confidence": meta.get("confidence"), "jev_ms": meta.get("latency_ms"),
        }
        return option["move"], tag
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


def placement(snapshot: dict[str, Any]) -> dict[int, chess.Piece]:
    out: dict[int, chess.Piece] = {}
    for p in snapshot["pieces"]:
        color = chess.WHITE if p["piece"][0] == "w" else chess.BLACK
        ptype = {"p": chess.PAWN, "n": chess.KNIGHT, "b": chess.BISHOP, "r": chess.ROOK, "q": chess.QUEEN, "k": chess.KING}[p["piece"][1]]
        out[chess.square(p["file"] - 1, p["rank"] - 1)] = chess.Piece(ptype, color)
    return out


def sync(board: chess.Board, observed: dict[int, chess.Piece]) -> tuple[chess.Board, chess.Move | None]:
    """Bring ``board`` up to the observed placement. If exactly one legal move explains the difference
    (the opponent's move, or our own move registering), push it. Returns (board, move_pushed)."""
    if board.piece_map() == observed:
        return board, None
    for move in list(board.legal_moves):
        board.push(move)
        if board.piece_map() == observed:
            return board, move
        board.pop()
    # Two plies at once, or a move we could not match: rebuild from the placement. The side that was to move
    # has moved (that is why the board changed), so the turn flips.
    rebuilt = chess.Board(None)
    for sq, piece in observed.items():
        rebuilt.set_piece_at(sq, piece)
    rebuilt.turn = not board.turn
    try:
        rebuilt.set_castling_fen(board.castling_xfen() if board.piece_map() else "-")
    except ValueError:
        rebuilt.set_castling_fen("-")
    return rebuilt, None


def play(browser: Any, max_moves: int = 200, on_step: Callable[[dict[str, Any]], None] | None = None,
         stop: Callable[[], bool] | None = None, think_s: float = 0.6, style: str | None = None,
         speak: Callable[[str], None] | None = None, speaking: Callable[[], bool] | None = None,
         move_delay: float | None = None) -> dict[str, Any]:
    """Loop: keep the position in code, infer the opponent's move from the board, choose, click, wait."""
    session = browser.session
    played: list[dict[str, Any]] = []
    snap = read(session)
    if not snap or not snap["pieces"]:
        return {"result": "no board", "moves": played}
    us = our_color(snap)
    board = position(snap)
    observed = placement(snap)
    if board.piece_map() != observed:
        board, _ = sync(board, observed)
    if not moves_of(snap) and board.piece_map() != chess.Board().piece_map():
        board.turn = us  # joined mid-game without a move list: the caller says it is our move
    idle = 0.0
    misses = 0
    while len(played) < max_moves:
        if stop and stop():
            break
        snap = read(session)
        if not snap or not snap["pieces"]:
            time.sleep(0.5)
            continue
        if snap["over"]:
            return {"result": "game over", "moves": played, "final": board.fen()}
        board, pushed = sync(board, placement(snap))
        if pushed is not None and board.turn == us:
            board.pop()
            their_san = board.san(pushed)
            board.push(pushed)
            print(f"  ♟ opponent: {their_san}", flush=True)
            if on_step:
                on_step({"action": {"label": f"…{their_san}", "kind": "chess_opponent", "key": None}, "operation": "OPPONENT", "text": None,
                         "step": len(played), "fen": board.fen()})
        if board.is_game_over():
            return {"result": board.result(claim_draw=True), "moves": played, "final": board.fen()}
        if board.turn != us:
            time.sleep(0.35)
            idle += 0.35
            if idle > 25 and pushed is None and board.piece_map() == placement(snap):
                # Nothing has changed for a while while we think it is their turn: we may have lost sync.
                # Rebuild from the board and assume it is ours (an illegal click costs nothing; a lost game does).
                rebuilt = chess.Board(None)
                for sq, piece in placement(snap).items():
                    rebuilt.set_piece_at(sq, piece)
                rebuilt.turn = us
                rebuilt.set_castling_fen("-")
                board = rebuilt
                idle = 0.0
                print("  ♟ resync: assuming it is our move", flush=True)
                continue
            if idle > 600:
                return {"result": "opponent idle", "moves": played}
            continue
        idle = 0.0
        t0 = time.time()
        move, engine = choose_move(board, think_s, style or os.environ.get("CHESS_STYLE") or None)
        san = board.san(move)
        print(f"  ♟ thinking done in {time.time() - t0:.1f}s → {san}", flush=True)
        decision = dict(LAST_DECISION)
        narration, why = ("", {})
        if decision.get("candidates") and os.environ.get("CHESS_TEACH", "1") not in ("0", "false", "no"):
            option = next((o for o in decision["candidates"] if o["san"] == san), decision["candidates"][0])
            try:
                narration, why = teaching_line(board, move, [{"san": o["san"], "eval": o["eval"], "cp": o["cp"], "rank": o["rank"], "line": o["line"]}
                                                             for o in decision["candidates"]], {**option, "move": move})
            except Exception as error:  # noqa: BLE001
                narration = f"{san}. Stockfish rates it {option['eval']}."
                why = {"error": str(error)[:80]}
            if speak:
                speak(narration)
        # Pause before moving: a human-like delay (CHESS_MOVE_DELAY seconds ± 30%), and in learning mode wait
        # until the narration has finished speaking so the point lands before the piece moves.
        delay = move_delay if move_delay is not None else float(os.environ.get("CHESS_MOVE_DELAY", "5"))
        target = time.monotonic() + delay * random.uniform(0.7, 1.3)
        hard_cap = time.monotonic() + 25
        time.sleep(0.4)  # let the speaker start before we ask whether it is speaking
        while time.monotonic() < hard_cap and (time.monotonic() < target or (speaking and speaking())):
            if stop and stop():
                break
            time.sleep(0.2)
        if not play_move(session, snap, move):
            misses += 1
            print(f"  ♟ {san} did not register ({misses}); re-reading", flush=True)
            if misses >= 3:
                return {"result": "board not accepting moves", "moves": played}
            time.sleep(0.6)
            continue
        misses = 0
        board.push(move)
        played.append({"ply": board.ply(), "san": san, "engine": engine, "fen": board.fen()})
        if on_step:
            on_step({"action": {"label": f"{san} ({engine})", "kind": "chess", "key": None}, "operation": "MOVE", "text": None,
                     "step": len(played), "fen": board.fen(), "decision": decision, "narration": narration, "why": why})
        print(f"  ♟ {len(played)}. {san}  [{engine}]", flush=True)
    return {"result": "move limit", "moves": played}
