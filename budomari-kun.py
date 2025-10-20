import streamlit as st
import matplotlib.pyplot as plt
from io import BytesIO
from dataclasses import dataclass
from typing import List, Tuple, Optional

st.set_page_config(page_title="板取り最適化（完全ギロチン・衝突回避）", layout="wide")

# -----------------------------
# 基本設定
# -----------------------------
BOARD_SIZES = {
    "サブロク (1820×910mm)": (1820.0, 910.0),
    "シハチ (2400×1200mm)": (2400.0, 1200.0),
    "ゴシ (1500×900mm)": (1500.0, 900.0),
}
EPS = 1e-6

@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

@dataclass
class PiecePlaced:
    pid: int
    x: float
    y: float
    w: float
    h: float
    rot: bool  # 回転したか
    # どのボードかは上位で管理

@dataclass
class CutLine:
    x1: float
    y1: float
    x2: float
    y2: float  # 垂直or水平の全通線

# -----------------------------
# UI
# -----------------------------
st.title("🪵 板取り最適化（完全ギロチン・衝突回避）")

left, right = st.columns([0.55, 0.45])
with left:
    st.subheader("① 条件入力")
    board_name = st.selectbox("母材サイズ", list(BOARD_SIZES.keys()))
    BOARD_W, BOARD_H = BOARD_SIZES[board_name]

    c1, c2, c3 = st.columns(3)
    allow_rotate = c1.checkbox("回転（90°）を許可", value=True)
    kerf = c2.number_input("刃厚（mm）", min_value=0.0, step=0.1, value=3.0, help="カット1本ごとに消費する幅/高さ")
    edge_trim = c3.number_input("外周安全マージン（mm）", min_value=0.0, step=0.5, value=5.0,
                                help="外周のNG帯（板サイズから左右上下で控え）")

    st.markdown("—")
    st.subheader("② 部材リスト（幅×高さ×枚数）")
    if "rows" not in st.session_state:
        st.session_state.rows = [
            {"w":300.0,"h":450.0,"n":2},
            {"w":600.0,"h":910.0,"n":1},
            {"w":200.0,"h":500.0,"n":3},
        ]

    to_delete = []
    for idx, row in enumerate(st.session_state.rows):
        a,b,c,d = st.columns([1,1,1,0.3])
        row["w"] = a.number_input(f"幅 w[{idx+1}] (mm)", min_value=1.0, step=1.0, value=float(row["w"]), key=f"w_{idx}")
        row["h"] = b.number_input(f"高 h[{idx+1}] (mm)", min_value=1.0, step=1.0, value=float(row["h"]), key=f"h_{idx}")
        row["n"] = int(c.number_input(f"枚数 n[{idx+1}]", min_value=1, step=1, value=int(row["n"]), key=f"n_{idx}"))
        if d.button("削除", key=f"del_{idx}"):
            to_delete.append(idx)
    if to_delete:
        st.session_state.rows = [r for i,r in enumerate(st.session_state.rows) if i not in to_delete]
    st.button("＋ 行を追加", on_click=lambda: st.session_state.rows.append({"w":300.0,"h":300.0,"n":1}))

    st.markdown("—")
    st.subheader("③ 最適化の目的")
    mode = st.radio(
        "目的を選択",
        ["① 歩留まり最優先（板枚数→廃材最少）", "② カット数最少（split本数最少）"],
        index=0
    )

    compute = st.button("板取りを計算")

# -----------------------------
# ユーティリティ
# -----------------------------
def expand_pieces(rows) -> List[Tuple[int, float, float]]:
    """同一サイズでも個片に展開（pid, w, h）"""
    out = []
    pid = 0
    for r in rows:
        w, h, n = float(r["w"]), float(r["h"]), int(r["n"])
        for _ in range(n):
            out.append((pid, w, h))
            pid += 1
    return out

def rect_fits(piece_w, piece_h, free: Rect):
    return piece_w <= free.w + EPS and piece_h <= free.h + EPS

def place_and_split(free: Rect, w:float, h:float, kerf:float, split_pref:str) -> Tuple[PiecePlaced, List[Rect], Optional[CutLine], Optional[CutLine]]:
    """
    free内の左上にw×hを置くと仮定（基準：左上基点で配置）。
    その後freeをギロチン分割する。
    split_pref: "min_waste", "min_cuts", "match_width_first", "match_height_first" などのヒント。
    戻り値: (placed, new_free_rects, vcut_line?, hcut_line?)
    カット線はfree領域内の分割線（板外までの延長は描画時に付与）。
    """
    # 配置位置（左上基点 → Streamlit描画は左下が原点なのでyはそのまま使いつつ後で反転しない）
    px, py = free.x, free.y
    placed = PiecePlaced(pid=-1, x=px, y=py, w=w, h=h, rot=False)

    # 分割方法（2分割×2で最大2本のカット）
    # 置いた直後の残りは「右」と「下」に分けられるのが基本（左上固定）
    # kerfは分割線の厚みとして減算
    right_w = max(0.0, free.w - w - kerf)   # 垂直スプリット分のkerf消費
    bottom_h = max(0.0, free.h - h - kerf)  # 水平スプリット分のkerf消費

    # 4パターン（どちらを先に切るかで出来る残り矩形が変わる）
    # A: 先に垂直（右側を作る）→ 次に水平（下側を作る）
    # B: 先に水平（下側）→ 次に垂直（右側）
    # どちらでも最終的な面積は同じことが多いが、kerf消費順序の差で微妙に変わる

    # 右側矩形
    right_rect_A = Rect(px + w + kerf, py, right_w, h) if right_w > EPS else None
    # 下側矩形
    bottom_rect_A = Rect(px, py + h + kerf, free.w, bottom_h) if bottom_h > EPS else None

    # パターンB（計算上は同じ寸法になるが、将来的な拡張で差を持たせやすいよう残しておく）
    right_rect_B = Rect(px + w + kerf, py, right_w, h) if right_w > EPS else None
    bottom_rect_B = Rect(px, py + h + kerf, free.w, bottom_h) if bottom_h > EPS else None

    # カット線（freeの内部線）※描画時に板外へ20mm延長する
    vcut = CutLine(px + w, py, px + w, py + h) if right_rect_A is not None else None
    hcut = CutLine(px, py + h, px + free.w, py + h) if bottom_rect_A is not None else None

    # split_prefの簡易ヒューリスティック
    if split_pref == "match_width_first" and right_rect_A is None and bottom_rect_A is not None:
        # 横ぴったり優先 → 高さ側だけ分割
        return placed, [bottom_rect_A], None, hcut
    if split_pref == "match_height_first" and bottom_rect_A is None and right_rect_A is not None:
        return placed, [right_rect_A], vcut, None

    # 既定：両方残るなら両方返す
    new_rects = []
    if right_rect_A is not None:
        new_rects.append(right_rect_A)
    if bottom_rect_A is not None:
        new_rects.append(bottom_rect_A)
    return placed, new_rects, vcut, hcut

def choose_score(free: Rect, w:float, h:float, kerf:float, objective:str) -> Tuple[float, str]:
    """
    候補freeに対してスコアを付ける。
    objective:
      - "yield": 廃材を最小化（= 右/下の合計残り面積が少ない、あるいはどちらかピッタリ）
      - "cuts": カット数最少（= 片側ピッタリで1本だけで済む配置を優先）
    戻り値: (低いほど良いscore, split_pref)
    """
    # どちらかピッタリ？
    width_exact = abs(free.w - w) <= EPS
    height_exact = abs(free.h - h) <= EPS

    # 残り幅/高
    right_w = max(0.0, free.w - w - kerf)
    bottom_h = max(0.0, free.h - h - kerf)
    waste = right_w * h + free.w * bottom_h  # 単純近似

    if objective == "cuts":
        # 片側がピッタリならスコアを強く優遇
        if width_exact or height_exact:
            return (0.0 if (width_exact and height_exact) else 0.1), ("match_width_first" if width_exact else "match_height_first")
        # それ以外は廃材少なめを優先
        return (1.0 + waste), "min_cuts"

    # yield
    if width_exact or height_exact:
        return (0.1 + waste), ("match_width_first" if width_exact else "match_height_first")
    return waste, "min_waste"

def guillotine_pack(pieces: List[Tuple[int,float,float]],
                    board_w: float, board_h: float,
                    kerf: float, edge: float,
                    allow_rotate: bool,
                    objective: str):
    """
    完全ギロチン二分割のヒューリスティック実装。
    - pieces: [(pid, w, h)]
    - objective: "yield" or "cuts"
    戻り: boards(list) それぞれ {placed:[PiecePlaced...], cuts:[CutLine...] }
    """
    # 有効サイズ
    eff_w = board_w - 2*edge
    eff_h = board_h - 2*edge
    if eff_w <= 0 or eff_h <= 0:
        return [], eff_w, eff_h

    # 面積降順
    pieces_sorted = sorted(pieces, key=lambda t: t[1]*t[2], reverse=True)

    boards = []

    def new_board():
        return {
            "free": [Rect(edge, edge, eff_w, eff_h)],   # 有効領域をfreeとして開始
            "placed": [],
            "cuts": []
        }

    cur = new_board()
    boards.append(cur)

    for pid, w0, h0 in pieces_sorted:
        placed_flag = False
        # 全ボード探索（First-Fit だと枚数が増えがち→Best-Fit風にscore最小を選ぶ）
        best_choice = None  # (score, b_idx, f_idx, rot, split_pref)
        for b_idx, b in enumerate(boards):
            for f_idx, free in enumerate(b["free"]):
                # 回転候補を試す
                for rot in ([False, True] if allow_rotate else [False]):
                    w = h0 if rot else w0
                    h = w0 if rot else h0
                    if not rect_fits(w, h, free):
                        continue
                    score, split_pref = choose_score(free, w, h, kerf, objective)
                    if best_choice is None or score < best_choice[0]:
                        best_choice = (score, b_idx, f_idx, rot, split_pref, w, h)

        if best_choice is None:
            # 新しいボードを開いてそこへ
            cur = new_board()
            boards.append(cur)
            # ここで必ず入るはず（最初のfreeは母材有効領域）
            w, h = (h0, w0) if (allow_rotate and h0<=eff_w and w0<=eff_h and h0*w0>w0*h0) else (w0, h0)
            # ただし入りきらない寸法があればエラー
            if w > eff_w + EPS or h > eff_h + EPS:
                st.error(f"部材#{pid}（{w0}×{h0}）が母材有効領域（{eff_w}×{eff_h}）に入りません。")
                return [], eff_w, eff_h

            # 最初のfreeへ配置
            free = cur["free"].pop(0)
            score, split_pref = choose_score(free, w, h, kerf, objective)
            placed, new_rects, vcut, hcut = place_and_split(free, w, h, kerf, split_pref)
            placed.pid = pid
            cur["placed"].append(placed)
            cur["free"].extend(new_rects)
            if vcut: cur["cuts"].append(vcut)
            if hcut: cur["cuts"].append(hcut)
            continue

        # 既存ボードのベストへ配置
        _, b_idx, f_idx, rot, split_pref, w, h = best_choice
        b = boards[b_idx]
        free = b["free"].pop(f_idx)
        placed, new_rects, vcut, hcut = place_and_split(free, w, h, kerf, split_pref)
        placed.pid = pid
        placed.rot = rot
        b["placed"].append(placed)
        b["free"].extend(new_rects)
        if vcut: b["cuts"].append(vcut)
        if hcut: b["cuts"].append(hcut)

    return boards, eff_w, eff_h

def build_lines_extended(cuts: List[CutLine], edge: float, eff_w: float, eff_h: float) -> List[CutLine]:
    """描画用：カット線を板外（有効外周）から20mmはみ出して表示。外周一致線は既に存在しない構成。"""
    out = []
    over = 20.0
    for c in cuts:
        if abs(c.x1 - c.x2) < EPS:
            # 縦
            out.append(CutLine(c.x1, edge - over, c.x2, edge + eff_h + over))
        else:
            # 横
            out.append(CutLine(edge - over, c.y1, edge + eff_w + over, c.y2))
    return out

def compute_metrics(boards, eff_w, eff_h):
    used_area_total = 0.0
    for b in boards:
        for p in b["placed"]:
            used_area_total += p.w * p.h
    eff_area_total = eff_w * eff_h * len(boards)
    yield_ratio = (used_area_total / eff_area_total * 100.0) if eff_area_total>0 else 0.0
    # 分割本数（cut線本数）
    cut_count = sum(len(b["cuts"]) for b in boards)
    return used_area_total, eff_area_total, yield_ratio, cut_count

def draw_boards(boards, title, board_w, board_h, edge, eff_w, eff_h):
    figs = []
    for i, b in enumerate(boards):
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.set_title(f"{title} - 板 {i+1}")
        ax.set_xlim(0, board_w)
        ax.set_ylim(0, board_h)
        # 母材外形
        ax.add_patch(plt.Rectangle((0,0), board_w, board_h, fill=False, linewidth=2))
        # 有効外周
        ax.add_patch(plt.Rectangle((edge, edge), eff_w, eff_h, fill=False, linestyle="--", linewidth=1))

        # ピース
        for p in b["placed"]:
            ax.add_patch(plt.Rectangle((p.x, p.y), p.w, p.h, fill=None, linewidth=1))
            ax.text(p.x + p.w/2, p.y + p.h/2, f"{int(p.w)}×{int(p.h)}\n#{p.pid}{'R' if p.rot else ''}",
                    ha="center", va="center", fontsize=8)

        # カット線（延長）
        cuts_ext = build_lines_extended(b["cuts"], edge, eff_w, eff_h)
        for c in cuts_ext:
            ax.plot([c.x1, c.x2], [c.y1, c.y2], linewidth=1.2)

        ax.set_aspect('equal')
        st.pyplot(fig)

        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
        buf.seek(0)
        st.download_button(
            label=f"⬇️ 板 {i+1} をPNG保存",
            data=buf,
            file_name=f"{title}_board{i+1}.png",
            mime="image/png"
        )
        figs.append(fig)
    return figs

def verify_no_overlap_and_no_cut_cross(boards):
    """
    念のため検証：
      - 部材同士の重なり無し
      - カット線が部材内部を横断しない（ギロチン手順上起きないが保険）
    """
    overlaps = 0
    cut_cross = 0
    for b in boards:
        P = b["placed"]
        # 重なり
        for i in range(len(P)):
            for j in range(i+1, len(P)):
                a, c = P[i], P[j]
                if (a.x + a.w - EPS > c.x) and (c.x + c.w - EPS > a.x) and (a.y + a.h - EPS > c.y) and (c.y + c.h - EPS > a.y):
                    overlaps += 1
        # カット横断
        for cl in b["cuts"]:
            vertical = abs(cl.x1 - cl.x2) < EPS
            for p in P:
                if vertical:
                    if p.x + EPS < cl.x1 < p.x + p.w - EPS and p.y + EPS < cl.y1 < p.y + p.h - EPS:
                        cut_cross += 1
                else:
                    if p.y + EPS < cl.y1 < p.y + p.h - EPS and p.x + EPS < cl.x1 < p.x + p.w - EPS:
                        cut_cross += 1
    return overlaps, cut_cross

# -----------------------------
# 実行
# -----------------------------
with right:
    st.subheader("④ 結果")
    if compute:
        rows = st.session_state.rows
        pieces = expand_pieces(rows)

        # 目的に合わせて2案を作る
        objA = "yield"
        objB = "cuts"

        boards_A, eff_w, eff_h = guillotine_pack(pieces, BOARD_W, BOARD_H, kerf, edge_trim, allow_rotate, objA)
        boards_B, _, _         = guillotine_pack(pieces, BOARD_W, BOARD_H, kerf, edge_trim, allow_rotate, objB)

        if not boards_A:
            st.error("有効領域が0です。外周安全マージンの値などを見直してください。")
        else:
            # A: 歩留まり
            usedA, effA, ratioA, cutsA = compute_metrics(boards_A, eff_w, eff_h)
            st.markdown("**① 歩留まり最優先**")
            st.write(f"- 必要枚数：{len(boards_A)} 枚")
            st.write(f"- 歩留まり（有効面積ベース）：{ratioA:.1f}%")
            st.write(f"- カット本数（内部スプリット数）：{cutsA}")
            ovA, cxA = verify_no_overlap_and_no_cut_cross(boards_A)
            if ovA==0 and cxA==0:
                st.success("検証OK：重なり・カットライン干渉なし（完全ギロチン）")
            else:
                st.warning(f"検証: 重なり={ovA} / カット横断={cxA}")
            draw_boards(boards_A, "歩留まり最優先", BOARD_W, BOARD_H, edge_trim, eff_w, eff_h)

            st.markdown("---")

            # B: カット最少
            usedB, effB, ratioB, cutsB = compute_metrics(boards_B, eff_w, eff_h)
            st.markdown("**② カット数最少化**")
            st.write(f"- 必要枚数：{len(boards_B)} 枚")
            st.write(f"- 歩留まり（有効面積ベース）：{ratioB:.1f}%")
            st.write(f"- カット本数（内部スプリット数）：{cutsB}")
            ovB, cxB = verify_no_overlap_and_no_cut_cross(boards_B)
            if ovB==0 and cxB==0:
                st.success("検証OK：重なり・カットライン干渉なし（完全ギロチン）")
            else:
                st.warning(f"検証: 重なり={ovB} / カット横断={cxB}")
            draw_boards(boards_B, "カット数最少", BOARD_W, BOARD_H, edge_trim, eff_w, eff_h)
    else:
        st.info("左で条件・部材を入力し「板取りを計算」を押してください。")
