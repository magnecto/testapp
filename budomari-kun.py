import streamlit as st
import matplotlib.pyplot as plt
from io import BytesIO
from collections import defaultdict
import math

st.set_page_config(page_title="板取り最適化（ギロチン＋自動探索＋検証）", layout="wide")

# ------------------------------------------------------------
# 基本設定
# ------------------------------------------------------------
BOARD_SIZES = {
    "サブロク (1820×910mm)": (1820.0, 910.0),
    "シハチ (2400×1200mm)": (2400.0, 1200.0),
    "ゴシ (1500×900mm)": (1500.0, 900.0),
}
EPS = 1e-6

# ------------------------------------------------------------
# UI（左：条件、右：結果）
# ------------------------------------------------------------
st.title("🪵 板取り最適化ツール（ギロチン＋自動探索＋同幅束＋検証）")
left, right = st.columns([0.55, 0.45])

with left:
    st.subheader("① 条件入力")
    board_name = st.selectbox("母材サイズ", list(BOARD_SIZES.keys()))
    BOARD_W, BOARD_H = BOARD_SIZES[board_name]

    c1, c2, c3 = st.columns(3)
    allow_rotate = c1.checkbox("回転（90°）を許可", value=True)
    kerf = c2.number_input("刃厚（mm）", min_value=0.0, step=0.1, value=3.0,
                           help="カットラインの実損失。歩留まりと配置に反映")
    edge_trim = c3.number_input("外周安全マージン（mm）", min_value=0.0, step=0.5, value=5.0,
                                help="外周のNG帯。板サイズから左右上下で控えます")

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
        max_height_candidates = c1.slider("棚高さ/カラム幅の候補数", 3, 20, 8)
        merge_height_tol = c2.number_input("候補マージ閾値（mm）", min_value=0.0, step=0.5, value=2.0)
    else:
        max_height_candidates = 8
        merge_height_tol = 2.0

    compute = st.button("板取りを計算")

# ------------------------------------------------------------
# ユーティリティ
# ------------------------------------------------------------
def normalize_dim(w,h,rotate_ok):
    return (min(w,h), max(w,h)) if rotate_ok else (w,h)

def expanded_pieces(rows, rotate_ok):
    """同一サイズで束ねず、n枚をすべて個片として展開（←ご要望）"""
    pieces = []
    pid = 0
    for r in rows:
        w,h,n = float(r["w"]), float(r["h"]), int(r["n"])
        for _ in range(n):
            pieces.append((w,h,False,pid))  # (w,h,rot,pid)
            pid += 1
    return pieces

def candidate_heights(rows, rotate_ok, max_k=8, tol=2.0):
    vals = []
    for r in rows:
        w,h,n = float(r["w"]), float(r["h"]), int(r["n"])
        vals.append(h)
        if rotate_ok: vals.append(w)
    if not vals: return [100.0]
    vals = sorted(vals)
    merged = []
    for v in vals:
        if not merged or abs(merged[-1]-v) > tol:
            merged.append(v)
    merged = sorted(merged, reverse=True)[:max_k]
    return merged

def same_width_pack_order(pieces):
    """棚内ローカル最適化：同幅束→幅大優先"""
    buckets = defaultdict(list)
    for w,h,rot,pid in pieces:
        buckets[w].append((w,h,rot,pid))
    ordered = []
    for w in sorted(buckets.keys(), reverse=True):
        ordered.extend(buckets[w])
    return ordered

def same_height_pack_order(pieces):
    """カラム内ローカル最適化：同高さ束→高さ大優先"""
    buckets = defaultdict(list)
    for w,h,rot,pid in pieces:
        buckets[h].append((w,h,rot,pid))
    ordered = []
    for h in sorted(buckets.keys(), reverse=True):
        ordered.extend(buckets[h])
    return ordered

# ------------------------------------------------------------
# ギロチン（横先行：リップ→クロス）
# ------------------------------------------------------------
def pack_horizontal(rows, BOARD_W, BOARD_H, eff_w, eff_h, shelf_h, kerf, edge_trim, rotate_ok):
    unit = kerf
    boards, used_area = [], 0.0
    # 個片展開
    items = expanded_pieces(rows, rotate_ok)
    # 棚高さに合うよう回転を検討（個片ごとに）
    adj = []
    for (w,h,rot,pid) in items:
        ww,hh,rr = w,h,rot
        if rotate_ok and min(w,h) <= shelf_h < max(w,h):
            ww,hh,rr = (min(w,h), max(w,h), (w>h))
        adj.append((ww,hh,rr,pid))
    # 面積大優先
    adj.sort(key=lambda t: t[0]*t[1], reverse=True)

    def new_board():
        return {"shelves":[], "used_h":0.0}
    cur = new_board(); boards.append(cur)

    def open_new_shelf():
        y = cur["used_h"] + (unit if cur["shelves"] else 0.0)
        sh = {"y":y, "h":shelf_h, "cursor_x":0.0, "placed":0, "pieces":[]}
        cur["shelves"].append(sh)
        cur["used_h"] = y + shelf_h

    open_new_shelf()

    for w,h,rot,pid in adj:
        placed = False
        # 既存棚へ
        for sh in cur["shelves"]:
            if h > sh["h"]: 
                continue
            need_w = w + (unit if sh["placed"]>0 else 0.0)
            remain = eff_w - sh["cursor_x"]
            if remain >= need_w:
                sh["pieces"].append((w,h,rot,pid))
                sh["placed"] += 1
                sh["cursor_x"] += (need_w if sh["placed"]>1 else w)
                used_area += w*h
                placed = True
                break
        if placed: continue

        # 新棚を同板内で作れるか？無理なら新板
        need_h = (unit if cur["shelves"] else 0.0) + shelf_h
        if cur["used_h"] + need_h > eff_h:
            cur = new_board(); boards.append(cur)
            open_new_shelf()
        else:
            open_new_shelf()

        sh = cur["shelves"][-1]
        sh["pieces"].append((w,h,rot,pid))
        sh["placed"] += 1
        sh["cursor_x"] += w
        used_area += w*h

    # 棚内ローカル最適化（同幅束）
    boards_draw = []
    for b in boards:
        pts = []
        for sh in b["shelves"]:
            ordered = same_width_pack_order(sh["pieces"])
            cursor_x = 0.0
            prev_w = None
            for i,(w,h,rot,pid) in enumerate(ordered):
                if i>0 and prev_w != w:
                    cursor_x += kerf  # 束の境はクロス1本ぶんの損失
                x = cursor_x
                y = sh["y"]
                pts.append((x+edge_trim, y+edge_trim, w, h, rot, pid))
                cursor_x += w
                prev_w = w
        boards_draw.append(pts)
    return boards_draw, used_area, len(boards)

# ------------------------------------------------------------
# ギロチン（縦先行：クロス→リップ）
# ------------------------------------------------------------
def pack_vertical(rows, BOARD_W, BOARD_H, eff_w, eff_h, col_w, kerf, edge_trim, rotate_ok):
    unit = kerf
    boards, used_area = [], 0.0
    items = expanded_pieces(rows, rotate_ok)
    adj = []
    for (w,h,rot,pid) in items:
        ww,hh,rr = w,h,rot
        if rotate_ok and min(w,h) <= col_w < max(w,h):
            ww,hh,rr = (min(w,h), max(w,h), (w>h))
        adj.append((ww,hh,rr,pid))
    adj.sort(key=lambda t: t[0]*t[1], reverse=True)

    def new_board():
        return {"cols":[], "used_w":0.0}
    cur = new_board(); boards.append(cur)

    def open_new_col():
        x = cur["used_w"] + (unit if cur["cols"] else 0.0)
        col = {"x":x, "w":col_w, "cursor_y":0.0, "placed":0, "pieces":[]}
        cur["cols"].append(col)
        cur["used_w"] = x + col_w

    open_new_col()

    for w,h,rot,pid in adj:
        placed = False
        for col in cur["cols"]:
            if w > col["w"]: 
                continue
            need_h = h + (unit if col["placed"]>0 else 0.0)
            remain = eff_h - col["cursor_y"]
            if remain >= need_h:
                col["pieces"].append((w,h,rot,pid))
                col["placed"] += 1
                col["cursor_y"] += (need_h if col["placed"]>1 else h)
                used_area += w*h
                placed = True
                break
        if placed: continue

        need_w = (unit if cur["cols"] else 0.0) + col_w
        if cur["used_w"] + need_w > eff_w:
            cur = new_board(); boards.append(cur)
            open_new_col()
        else:
            open_new_col()

        col = cur["cols"][-1]
        col["pieces"].append((w,h,rot,pid))
        col["placed"] += 1
        col["cursor_y"] += h
        used_area += w*h

    # カラム内ローカル最適化（同高さ束）
    boards_draw = []
    for b in boards:
        pts = []
        for col in b["cols"]:
            ordered = same_height_pack_order(col["pieces"])
            cursor_y = 0.0
            prev_h = None
            for i,(w,h,rot,pid) in enumerate(ordered):
                if i>0 and prev_h != h:
                    cursor_y += kerf  # 束の境はリップ1本ぶん
                x = col["x"]
                y = cursor_y
                pts.append((x+edge_trim, y+edge_trim, w, h, rot, pid))
                cursor_y += h
                prev_h = h
        boards_draw.append(pts)
    return boards_draw, used_area, len(boards)

# ------------------------------------------------------------
# カットライン生成と検証
#   - 各部材の四辺を延長した全通線をカットライン候補に
#   - 外周に一致する線は省略
#   - 交差検証：重なり／カットラインが他部材の内部を横断しないか
# ------------------------------------------------------------
def dedup(values, tol=1e-4):
    values = sorted(values)
    out = []
    for v in values:
        if not out or abs(out[-1]-v) > tol:
            out.append(v)
    return out

def build_cutlines_from_pieces(boards_draw, BOARD_W, BOARD_H, eff_w, eff_h, edge_trim):
    """四辺延長 → 盤全面の全通カットライン（外周一致は除外）"""
    all_blocks = []
    for pieces in boards_draw:
        # 1) 重なり検査（個片単位）
        overlaps = []
        for i in range(len(pieces)):
            xi, yi, wi, hi, _, pidi = pieces[i]
            for j in range(i+1, len(pieces)):
                xj, yj, wj, hj, _, pidj = pieces[j]
                if (xi + wi - EPS > xj) and (xj + wj - EPS > xi) and (yi + hi - EPS > yj) and (yj + hj - EPS > yi):
                    overlaps.append((pidi, pidj))

        # 2) 候補カットライン
        xs, ys = [], []
        for (x,y,w,h,rot,pid) in pieces:
            x1, x2 = x, x + w
            y1, y2 = y, y + h
            # 外周一致は省略
            if abs(x1 - edge_trim) > EPS: xs.append(x1)
            if abs(x2 - (edge_trim + eff_w)) > EPS: xs.append(x2)
            if abs(y1 - edge_trim) > EPS: ys.append(y1)
            if abs(y2 - (edge_trim + eff_h)) > EPS: ys.append(y2)

        xs = dedup(xs); ys = dedup(ys)

        # 3) ライン→部材内部横断の検査
        #    縦線 x=c が他部材の内部 (x1<c<x2) を通るとNG（辺上はOK）
        v_conflicts = []
        for c in xs:
            for (x,y,w,h,rot,pid) in pieces:
                if x + EPS < c < x + w - EPS:
                    v_conflicts.append(("V", c, pid))
        h_conflicts = []
        for r in ys:
            for (x,y,w,h,rot,pid) in pieces:
                if y + EPS < r < y + h - EPS:
                    h_conflicts.append(("H", r, pid))

        # 4) 描画用ライン（板外に20mmはみ出し）
        over = 20.0
        cutlines = []
        for c in xs:
            cutlines.append((c, -over + edge_trim, c, eff_h + edge_trim + over, "V"))
        for r in ys:
            cutlines.append((-over + edge_trim, r, eff_w + edge_trim + over, r, "H"))

        all_blocks.append({
            "pieces": pieces,
            "cutlines": cutlines,
            "overlaps": overlaps,
            "v_conflicts": v_conflicts,
            "h_conflicts": h_conflicts,
            "cuts_count": len(xs) + len(ys)
        })
    return all_blocks

def draw_block(block, title, i, BOARD_W, BOARD_H):
    fig, ax = plt.subplots(figsize=(9,4.5))
    ax.set_title(f"{title} - 板 {i+1}")
    ax.set_xlim(0, BOARD_W)
    ax.set_ylim(0, BOARD_H)
    ax.add_patch(plt.Rectangle((0,0), BOARD_W, BOARD_H, fill=False, linewidth=2))

    for (x,y,w,h,rot,pid) in block["pieces"]:
        ax.add_patch(plt.Rectangle((x,y), w, h, fill=None, linewidth=1))
        ax.text(x+w/2, y+h/2, f"{int(w)}×{int(h)}\n#{pid}", ha="center", va="center", fontsize=8)

    for (x1,y1,x2,y2,kind) in block["cutlines"]:
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

# ------------------------------------------------------------
# 総合探索（横先行＆縦先行）
#   - 歩留まり最優先：枚数→廃材（eff*boards - used）
#   - カット数最少：カット本数→枚数
# ------------------------------------------------------------
def explore(rows, BOARD_W, BOARD_H, kerf, edge_trim, rotate_ok, max_k, merge_tol):
    eff_w = BOARD_W - 2*edge_trim
    eff_h = BOARD_H - 2*edge_trim
    if eff_w <= 0 or eff_h <= 0:
        return None, None, eff_w, eff_h

    heights = candidate_heights(rows, rotate_ok, max_k, merge_tol)

    # 横候補
    horiz = []
    for sh in heights:
        bd, used, boards_n = pack_horizontal(rows, BOARD_W, BOARD_H, eff_w, eff_h, sh, kerf, edge_trim, rotate_ok)
        waste = eff_w*eff_h*boards_n - used
        horiz.append({"mode":"H","param":sh,"boards_draw":bd,"used":used,"waste":waste,"boards":boards_n})

    # 縦候補（高さ候補をそのまま幅候補へ）
    vert = []
    for cw in heights:
        bd, used, boards_n = pack_vertical(rows, BOARD_W, BOARD_H, eff_w, eff_h, cw, kerf, edge_trim, rotate_ok)
        waste = eff_w*eff_h*boards_n - used
        vert.append({"mode":"V","param":cw,"boards_draw":bd,"used":used,"waste":waste,"boards":boards_n})

    allc = horiz + vert

    best_yield = sorted(allc, key=lambda c: (c["boards"], c["waste"]))[0]
    # カット数は四辺延長から算出するので、ここでは仮に最小を後で再評価
    # まず全候補に対してカット本数を計測
    def calc_cuts_count(block):
        blocks = build_cutlines_from_pieces(block["boards_draw"], BOARD_W, BOARD_H, eff_w, eff_h, edge_trim)
        return sum(b["cuts_count"] for b in blocks)

    for c in allc:
        c["cuts_count"] = calc_cuts_count(c)
    best_cut = sorted(allc, key=lambda c: (c["cuts_count"], c["boards"]))[0]

    return best_yield, best_cut, eff_w, eff_h

# ------------------------------------------------------------
# 実行・表示
# ------------------------------------------------------------
with right:
    st.subheader("③ 結果")
    if compute:
        rows = st.session_state.rows
        best_yield, best_cut, eff_w, eff_h = explore(
            rows, BOARD_W, BOARD_H, kerf, edge_trim, allow_rotate,
            max_height_candidates, merge_height_tol
        )
        if best_yield is None:
            st.error("安全マージンが大きすぎて有効領域がありません。")
        else:
            # ==== 歩留まり最優先 ====
            st.markdown("**① 歩留まり最優先**")
            st.write(f"- パターン：{'横先行' if best_yield['mode']=='H' else '縦先行'} / パラメータ={int(best_yield['param'])} mm")
            st.write(f"- 必要枚数：{best_yield['boards']} 枚")
            eff_total = eff_w*eff_h*best_yield['boards']
            st.write(f"- 歩留まり（有効面積ベース）：{(best_yield['used']/eff_total*100):.1f}%")

            blocks_A = build_cutlines_from_pieces(best_yield["boards_draw"], BOARD_W, BOARD_H, eff_w, eff_h, edge_trim)
            st.write(f"- 推定カット本数：{sum(b['cuts_count'] for b in blocks_A)}（四辺延長・外周省略後）")
            # 検証レポート
            overlap_cnt = sum(len(b["overlaps"]) for b in blocks_A)
            vconf_cnt = sum(len(b["v_conflicts"]) for b in blocks_A)
            hconf_cnt = sum(len(b["h_conflicts"]) for b in blocks_A)
            if overlap_cnt or vconf_cnt or hconf_cnt:
                st.warning(f"検証: 重なり={overlap_cnt} / 縦線干渉={vconf_cnt} / 横線干渉={hconf_cnt}")
            else:
                st.success("検証OK：重なり・カットライン干渉なし")

            for i, block in enumerate(blocks_A):
                draw_block(block, "歩留まり最優先", i, BOARD_W, BOARD_H)

            st.markdown("---")

            # ==== カット数最少 ====
            st.markdown("**② カットライン最少化**")
            st.write(f"- パターン：{'横先行' if best_cut['mode']=='H' else '縦先行'} / パラメータ={int(best_cut['param'])} mm")
            st.write(f"- 必要枚数：{best_cut['boards']} 枚")
            eff_total2 = eff_w*eff_h*best_cut['boards']
            st.write(f"- 歩留まり（有効面積ベース）：{(best_cut['used']/eff_total2*100):.1f}%")

            blocks_B = build_cutlines_from_pieces(best_cut["boards_draw"], BOARD_W, BOARD_H, eff_w, eff_h, edge_trim)
            st.write(f"- 推定カット本数：{sum(b['cuts_count'] for b in blocks_B)}（四辺延長・外周省略後）")
            overlap_cnt = sum(len(b["overlaps"]) for b in blocks_B)
            vconf_cnt = sum(len(b["v_conflicts"]) for b in blocks_B)
            hconf_cnt = sum(len(b["h_conflicts"]) for b in blocks_B)
            if overlap_cnt or vconf_cnt or hconf_cnt:
                st.warning(f"検証: 重なり={overlap_cnt} / 縦線干渉={vconf_cnt} / 横線干渉={hconf_cnt}")
            else:
                st.success("検証OK：重なり・カットライン干渉なし")

            for i, block in enumerate(blocks_B):
                draw_block(block, "カット数最少", i, BOARD_W, BOARD_H)
    else:
        st.info("左側で条件・部材を入力し、「板取りを計算」を押してください。")
