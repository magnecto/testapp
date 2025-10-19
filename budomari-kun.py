import streamlit as st
import matplotlib.pyplot as plt
from io import BytesIO
from collections import defaultdict, Counter
import math

st.set_page_config(page_title="板取り最適化（ギロチン＋自動探索）", layout="wide")

# ------------------------------------------------------------
# 基本設定
# ------------------------------------------------------------
BOARD_SIZES = {
    "サブロク (1820×910mm)": (1820.0, 910.0),
    "シハチ (2400×1200mm)": (2400.0, 1200.0),
    "ゴシ (1500×900mm)": (1500.0, 900.0),
}
BLADE_PRESSURE_PRESET = {
    "Low（精度重視）": 0.0,
    "Med（標準）": 0.5,
    "High（たわみ対策）": 1.0,
}

# ------------------------------------------------------------
# UI（左：条件、右：結果）
# ------------------------------------------------------------
st.title("🪵 板取り最適化ツール（ギロチン＋自動探索＋縦先行＋同幅束）")
left, right = st.columns([0.55, 0.45])

with left:
    st.subheader("① 条件入力")
    board_name = st.selectbox("母材サイズ", list(BOARD_SIZES.keys()))
    BOARD_W, BOARD_H = BOARD_SIZES[board_name]

    c1, c2, c3 = st.columns(3)
    allow_rotate = c1.checkbox("回転（90°）を許可", value=True)
    kerf = c2.number_input("刃厚（mm）", min_value=0.0, step=0.1, value=3.0)
    blade_pressure = c3.selectbox("刃圧", list(BLADE_PRESSURE_PRESET.keys()))
    extra_allow = BLADE_PRESSURE_PRESET[blade_pressure]

    edge_trim = st.number_input("外周安全マージン（mm）", min_value=0.0, step=0.5, value=5.0)

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
    advanced = st.checkbox("詳細オプションを表示", value=False)
    if advanced:
        c1, c2 = st.columns(2)
        # 候補棚高の最大数（大きいほど探索精度↑だが遅くなる）
        max_height_candidates = c1.slider("棚高さ候補の最大数", 3, 20, 8)
        # 似た高さをまとめる閾値（mm）
        merge_height_tol = c2.number_input("似た高さの統合閾値（mm）", min_value=0.0, step=0.5, value=2.0)
    else:
        max_height_candidates = 8
        merge_height_tol = 2.0

    compute = st.button("板取りを計算")

# ------------------------------------------------------------
# ユーティリティ
# ------------------------------------------------------------
def normalize_dim(w, h, rotate_ok):
    return (min(w,h), max(w,h)) if rotate_ok else (w,h)

def demand_bundled(rows, rotate_ok):
    """回転許可なら w/h を正規化して同寸束ね"""
    bundle = defaultdict(int)
    for r in rows:
        w,h,n = float(r["w"]), float(r["h"]), int(r["n"])
        key = normalize_dim(w,h,rotate_ok)
        bundle[key] += n
    # （w,h,cnt）のリストで返す
    return [(k[0], k[1], v) for k,v in bundle.items()]

def candidate_heights(rows, rotate_ok, max_k=8, tol=2.0):
    """棚高さの候補集合を生成（部材の高さ＋回転時の幅も含め、近い値はマージ）"""
    vals = []
    for r in rows:
        w,h,n = float(r["w"]), float(r["h"]), int(r["n"])
        vals.append(h)
        if rotate_ok: vals.append(w)
    vals = sorted(set(vals))
    # 近似値をまとめる
    merged = []
    for v in vals:
        if not merged or abs(merged[-1]-v) > tol:
            merged.append(v)
    # 上位から最大K個（大きめ優先→棚数が増えにくい傾向）
    merged = sorted(merged, reverse=True)[:max_k]
    return merged if merged else [max(1.0, max(vals) if vals else 100.0)]

def same_width_pack_order(pieces):
    """
    棚内ローカル最適化：同幅束をまとめて並べる順序に並び替え。
    pieces: [(w,h,rot_original_flag, id), ...]  ※idは図示管理用
    """
    buckets = defaultdict(list)
    for w,h,rot,idx in pieces:
        buckets[w].append((w,h,rot,idx))
    # 幅の大きいバケツから先に（縦カット本数をなるべく少なく）
    ordered = []
    for w in sorted(buckets.keys(), reverse=True):
        ordered.extend(buckets[w])
    return ordered

# ------------------------------------------------------------
# ギロチン・パッキング（横先行：リップ→クロス）
#   棚高さを固定値（candidate）として探索
# ------------------------------------------------------------
def pack_horizontal(rows, board_w, board_h, eff_w, eff_h, shelf_h, kerf, extra, edge_trim, rotate_ok):
    """横棚（固定高さ）で詰める。戻り値：(boards_draw, cuts_draw, used_area, cut_count)"""
    unit = kerf + extra
    boards = []
    cuts_all = []
    used_area = 0.0
    cut_count = 0

    # 準備：需要をサイズごとに展開（各ピース単位）
    expanded = []
    idx_counter = 0
    for w,h,n in demand_bundled(rows, rotate_ok):
        for _ in range(n):
            # 棚高に合わせて向きを選ぶ（回転可なら低い方が棚に乗りやすい）
            if rotate_ok and min(w,h) <= shelf_h < max(w,h):
                ww,hh,rot = (min(w,h), max(w,h), (w>h))
            else:
                ww,hh,rot = (w,h,False)
            expanded.append((ww,hh,rot, idx_counter))
            idx_counter += 1

    # 大きい面積から（棚を先に埋めやすく）
    expanded.sort(key=lambda t: t[0]*t[1], reverse=True)

    def new_board():
        return {"shelves":[], "used_h":0.0, "cuts":[]}

    cur = new_board()
    boards.append(cur)

    # 1) 棚を積む（リップ全通）
    shelf_idx = -1
    def open_new_shelf():
        nonlocal shelf_idx
        y = cur["used_h"] + (unit if cur["shelves"] else 0.0)
        sh = {"x":0.0,"y":y,"h":shelf_h,"cursor_x":0.0,"placed":0,"pieces":[]}
        cur["shelves"].append(sh)
        cur["used_h"] = y + shelf_h
        shelf_idx = len(cur["shelves"])-1
        # リップ線
        x1, x2 = 0.0 + edge_trim, eff_w + edge_trim
        yline = (edge_trim if len(cur["shelves"])==1 else y + edge_trim - unit)
        cur["cuts"].append((x1, yline, x2, yline, "horizontal"))

    open_new_shelf()

    for w,h,rot,pid in expanded:
        placed = False
        # 既存棚へ（同幅束最適化のために、後で並べ替えるのでひとまず候補棚だけ見つける）
        for sh in cur["shelves"]:
            # 高さチェック
            if h > sh["h"]: 
                continue
            need_w = w + (unit if sh["placed"]>0 else 0.0)
            remain = eff_w - sh["cursor_x"]
            if remain >= need_w:
                # 一旦置く（後で束最適並べ替えで位置確定）
                sh["pieces"].append((w,h,rot,pid))
                sh["placed"] += 1
                sh["cursor_x"] += need_w if sh["placed"]>1 else w
                used_area += w*h
                placed = True
                break

        if placed:
            continue

        # 新しい棚が今のボードに入るか
        need_h = (unit if cur["shelves"] else 0.0) + shelf_h
        if cur["used_h"] + need_h > eff_h:
            # 新しいボードへ
            cur = new_board()
            boards.append(cur)
            open_new_shelf()
        else:
            # 同ボード内に新棚を開く
            open_new_shelf()

        # その新棚へ置く
        sh = cur["shelves"][-1]
        sh["pieces"].append((w,h,rot,pid))
        sh["placed"] += 1
        sh["cursor_x"] += w
        used_area += w*h

    # 棚内ローカル最適化：同幅束をまとめ、縦カット（クロス）本数を最少化
    cuts_all = []
    over = 20.0
    for b in boards:
        # 再配置（Xを決め直す）
        for sh in b["shelves"]:
            ordered = same_width_pack_order(sh["pieces"])
            cursor_x = 0.0
            placed_rects = []
            prev_w = None
            for i,(w,h,rot,pid) in enumerate(ordered):
                # 同幅の先頭でクロスを1本入れる（最初の束は不要）
                if i>0:
                    if prev_w != w:
                        # クロス線
                        cx = cursor_x + edge_trim
                        b["cuts"].append((cx, sh["y"]+edge_trim, cx, sh["y"]+sh["h"]+edge_trim, "vertical"))
                        cut_count += 1
                        cursor_x += (kerf + extra)  # クロス刃厚ぶんだけ食う
                placed_rects.append((cursor_x, sh["y"], w, h, rot))
                cursor_x += w
                prev_w = w
            # 上書き
            sh["pieces_draw"] = [(x+edge_trim, y+edge_trim, w, h, rot) for (x,y,w,h,rot) in placed_rects]

        # リップ線の本数（棚数ぶん-最上段1本基準）：既に cuts に入っているのでカウント
        # ここでは描画時に一括
        pass

        # 描画用カットライン（はみ出し）
        for (x1,y1,x2,y2,kind) in b["cuts"]:
            if kind=="horizontal":
                cuts_all.append((-over+edge_trim, y1, eff_w+edge_trim+over, y1, kind))
            else:
                cuts_all.append((x1, -over+edge_trim, x1, eff_h+edge_trim+over, kind))

    # boards_draw へ整形
    boards_draw = []
    for b in boards:
        pts = []
        for sh in b["shelves"]:
            if "pieces_draw" in sh:
                pts.extend(sh["pieces_draw"])
        boards_draw.append(pts)

    # リップ線のカウント（棚数-1 本を有効線として数える/最上段は外周扱い）
    for b in boards:
        if len(b["shelves"])>=2:
            cut_count += (len(b["shelves"])-1)

    return boards_draw, cuts_all, used_area, cut_count, len(boards)

# ------------------------------------------------------------
# ギロチン・パッキング（縦先行：クロス→リップ）
#   仕組みは左右入れ替え。カラム幅（固定値）で探索。
# ------------------------------------------------------------
def pack_vertical(rows, board_w, board_h, eff_w, eff_h, col_w, kerf, extra, edge_trim, rotate_ok):
    """縦棚（固定幅）で詰める。戻り値は横版と同じ。"""
    unit = kerf + extra
    boards = []
    cuts_all = []
    used_area = 0.0
    cut_count = 0

    expanded = []
    idx = 0
    for w,h,n in demand_bundled(rows, rotate_ok):
        for _ in range(n):
            # カラム幅に合う向き
            if rotate_ok and min(w,h) <= col_w < max(w,h):
                ww,hh,rot = (min(w,h), max(w,h), (w>h))
            else:
                ww,hh,rot = (w,h,False)
            expanded.append((ww,hh,rot, idx))
            idx += 1
    expanded.sort(key=lambda t: t[0]*t[1], reverse=True)

    def new_board():
        return {"cols":[], "used_w":0.0, "cuts":[]}

    cur = new_board()
    boards.append(cur)

    def open_new_col():
        x = cur["used_w"] + (unit if cur["cols"] else 0.0)
        col = {"x":x,"y":0.0,"w":col_w,"cursor_y":0.0,"placed":0,"pieces":[]}
        cur["cols"].append(col)
        cur["used_w"] = x + col_w
        # 縦クロス線
        y1, y2 = 0.0 + edge_trim, eff_h + edge_trim
        xline = (edge_trim if len(cur["cols"])==1 else x + edge_trim - unit)
        cur["cuts"].append((xline, y1, xline, y2, "vertical"))

    open_new_col()

    for w,h,rot,pid in expanded:
        placed = False
        for col in cur["cols"]:
            if w > col["w"]:
                continue
            need_h = h + (unit if col["placed"]>0 else 0.0)
            remain = eff_h - col["cursor_y"]
            if remain >= need_h:
                col["pieces"].append((w,h,rot,pid))
                col["placed"] += 1
                col["cursor_y"] += need_h if col["placed"]>1 else h
                used_area += w*h
                placed = True
                break
        if placed:
            continue

        # 新しいカラムが入るか
        need_w = (unit if cur["cols"] else 0.0) + col_w
        if cur["used_w"] + need_w > eff_w:
            cur = new_board()
            boards.append(cur)
            open_new_col()
        else:
            open_new_col()

        col = cur["cols"][-1]
        col["pieces"].append((w,h,rot,pid))
        col["placed"] += 1
        col["cursor_y"] += h
        used_area += w*h

    # カラム内ローカル最適化：同高束（横版の同幅束の左右反転版）
    cuts_all = []
    over = 20.0
    for b in boards:
        for col in b["cols"]:
            # 同高さ束でまとめ、リップ（横線）本数を削減
            buckets = defaultdict(list)
            for (w,h,rot,pid) in col["pieces"]:
                buckets[h].append((w,h,rot,pid))
            ordered = []
            for hkey in sorted(buckets.keys(), reverse=True):
                ordered.extend(buckets[hkey])
            cursor_y = 0.0
            placed_rects = []
            prev_h = None
            for i,(w,h,rot,pid) in enumerate(ordered):
                if i>0 and prev_h != h:
                    # リップ線
                    ry = cursor_y + edge_trim
                    b["cuts"].append((edge_trim, ry, eff_w+edge_trim, ry, "horizontal"))
                    cut_count += 1
                    cursor_y += (kerf + extra)
                placed_rects.append((col["x"], cursor_y, w, h, rot))
                cursor_y += h
                prev_h = h
            col["pieces_draw"] = [(x+edge_trim, y+edge_trim, w, h, rot) for (x,y,w,h,rot) in placed_rects]

        for (x1,y1,x2,y2,kind) in b["cuts"]:
            if kind=="vertical":
                cuts_all.append((x1, -over+edge_trim, x1, eff_h+edge_trim+over, kind))
            else:
                cuts_all.append((-over+edge_trim, y1, eff_w+edge_trim+over, y1, kind))

    boards_draw = []
    for b in boards:
        pts = []
        for col in b["cols"]:
            if "pieces_draw" in col:
                pts.extend(col["pieces_draw"])
        boards_draw.append(pts)

    for b in boards:
        if len(b["cols"])>=2:
            cut_count += (len(b["cols"])-1)

    return boards_draw, cuts_all, used_area, cut_count, len(boards)

# ------------------------------------------------------------
# 総合探索：横先行（棚高候補）と縦先行（カラム幅候補）を両方評価
#   - 歩留まり最優先：枚数 → 捨て面積（eff-使用）で最良
#   - カット数最少：カット本数 → 枚数 で最良
# ------------------------------------------------------------
def explore_and_choose(rows, BOARD_W, BOARD_H, kerf, extra, edge, rotate_ok,
                       max_h_candidates=8, merge_tol=2.0):
    eff_w = BOARD_W - 2*edge
    eff_h = BOARD_H - 2*edge
    if eff_w<=0 or eff_h<=0:
        return None, None, None, None

    heights = candidate_heights(rows, rotate_ok, max_h_candidates, merge_tol)

    # 横先行（棚高固定）
    horiz_candidates = []
    for sh in heights:
        res = pack_horizontal(rows, BOARD_W, BOARD_H, eff_w, eff_h, sh, kerf, extra, edge, rotate_ok)
        boards_draw, cuts_draw, used, cut_n, boards_n = res
        waste = eff_w*eff_h*boards_n - used
        horiz_candidates.append({
            "mode":"H",
            "param":sh,
            "boards_draw":boards_draw,
            "cuts_draw":cuts_draw,
            "used":used,
            "waste":waste,
            "cuts":cut_n,
            "boards":boards_n
        })

    # 縦先行（カラム幅固定）→ 高さ候補をそのまま幅候補として流用
    vert_candidates = []
    for cw in heights:
        res = pack_vertical(rows, BOARD_W, BOARD_H, eff_w, eff_h, cw, kerf, extra, edge, rotate_ok)
        boards_draw, cuts_draw, used, cut_n, boards_n = res
        waste = eff_w*eff_h*boards_n - used
        vert_candidates.append({
            "mode":"V",
            "param":cw,
            "boards_draw":boards_draw,
            "cuts_draw":cuts_draw,
            "used":used,
            "waste":waste,
            "cuts":cut_n,
            "boards":boards_n
        })

    allc = horiz_candidates + vert_candidates

    # A) 歩留まり最優先（枚数 → waste）
    best_yield = sorted(allc, key=lambda c: (c["boards"], c["waste"]))[0]
    # B) カット数最少（cuts → boards）
    best_cut = sorted(allc, key=lambda c: (c["cuts"], c["boards"]))[0]

    return best_yield, best_cut, eff_w, eff_h

def draw_result(block, title, BOARD_W, BOARD_H):
    figs = []
    over = 20.0
    for i, pieces in enumerate(block["boards_draw"]):
        fig, ax = plt.subplots(figsize=(9,4.5))
        ax.set_title(f"{title} - 板 {i+1}  [{ '横先行' if block['mode']=='H' else '縦先行' }]  param={int(block['param'])}")
        ax.set_xlim(0, BOARD_W)
        ax.set_ylim(0, BOARD_H)
        ax.add_patch(plt.Rectangle((0,0), BOARD_W, BOARD_H, fill=False, linewidth=2))
        # ピース
        for (x,y,w,h,rot) in pieces:
            ax.add_patch(plt.Rectangle((x,y), w, h, fill=None, linewidth=1))
            ax.text(x+w/2, y+h/2, f"{int(w)}×{int(h)}", ha="center", va="center", fontsize=8)
        # カットライン
        for (x1,y1,x2,y2,kind) in block["cuts_draw"]:
            ax.plot([x1, x2], [y1, y2], linewidth=1.2)
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

with right:
    st.subheader("③ 結果")
    if compute:
        rows = st.session_state.rows
        best_yield, best_cut, eff_w, eff_h = explore_and_choose(
            rows, BOARD_W, BOARD_H, kerf, extra_allow, edge_trim, allow_rotate,
            max_h_candidates=max_height_candidates, merge_tol=merge_height_tol
        )
        if best_yield is None:
            st.error("安全マージンが大きすぎて有効領域がありません。")
        else:
            # A: 歩留まり最優先
            st.markdown("**① 歩留まり最優先**")
            st.write(f"- パターン：{'横先行' if best_yield['mode']=='H' else '縦先行'} / パラメータ={int(best_yield['param'])} mm")
            st.write(f"- 必要枚数：{best_yield['boards']} 枚")
            eff_area_total = (BOARD_W-2*edge_trim)*(BOARD_H-2*edge_trim)*best_yield['boards']
            st.write(f"- 歩留まり：{(best_yield['used']/eff_area_total*100):.1f}%")
            st.write(f"- カット本数（推定）：{best_yield['cuts']}")
            draw_result(best_yield, "歩留まり最優先", BOARD_W, BOARD_H)

            st.markdown("---")

            # B: カット数最少
            st.markdown("**② カットライン最少化**")
            st.write(f"- パターン：{'横先行' if best_cut['mode']=='H' else '縦先行'} / パラメータ={int(best_cut['param'])} mm")
            st.write(f"- 必要枚数：{best_cut['boards']} 枚")
            eff_area_total2 = (BOARD_W-2*edge_trim)*(BOARD_H-2*edge_trim)*best_cut['boards']
            st.write(f"- 歩留まり：{(best_cut['used']/eff_area_total2*100):.1f}%")
            st.write(f"- カット本数（推定）：{best_cut['cuts']}")
            draw_result(best_cut, "カット数最少", BOARD_W, BOARD_H)
    else:
        st.info("左側で条件・部材を入力し、「板取りを計算」を押してください。")
