import streamlit as st
import matplotlib.pyplot as plt
from io import BytesIO

# 定義：母材サイズ一覧
BOARD_SIZES = {
    "サブロク (1820×910mm)": (1820, 910),
    "シハチ (2400×1200mm)": (2400, 1200),
    "ゴシ (1500×900mm)": (1500, 900),
}

st.title("🪵 板取り最適化ツール")

# 板サイズ選択
board_name = st.selectbox("母材サイズを選択", list(BOARD_SIZES.keys()))
BOARD_W, BOARD_H = BOARD_SIZES[board_name]

# 部材入力
st.subheader("部材リスト入力")
count = st.number_input("登録する部材の種類数", min_value=1, max_value=20, value=3)
parts = []
for i in range(count):
    cols = st.columns(3)
    w = cols[0].number_input(f"部材{i+1} 幅(mm)", min_value=1, value=300)
    h = cols[1].number_input(f"部材{i+1} 高さ(mm)", min_value=1, value=450)
    n = cols[2].number_input(f"部材{i+1} 枚数", min_value=1, value=1)
    parts.append((w, h, n))

# 回転許可オプション
allow_rotate = st.checkbox("部材の回転（縦横入れ替え）を許可する", value=True)

# 板取りアルゴリズム
def pack_rectangles(parts, optimize_for="yield", rotate=True):
    rects = []
    x, y = 0, 0
    max_row_h = 0
    boards = [[]]

    for w, h, n in parts:
        for _ in range(n):
            placed = False
            for attempt in range(2):  # 回転試行
                if x + w <= BOARD_W and y + h <= BOARD_H:
                    placed = True
                    break
                elif rotate:
                    w, h = h, w  # 回転して再試行
            if not placed:
                # 次の行へ
                x = 0
                y += max_row_h
                max_row_h = 0
                if y + h > BOARD_H:
                    # 新しい板へ
                    boards.append([])
                    x, y, max_row_h = 0, 0, 0
            boards[-1].append((x, y, w, h))
            x += w
            max_row_h = max(max_row_h, h)
    return boards

# 図面描画関数
def draw_board(boards, title):
    figs = []
    for i, board in enumerate(boards):
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.set_title(f"{title} - 板 {i+1}")
        ax.set_xlim(0, BOARD_W)
        ax.set_ylim(0, BOARD_H)
        for (x, y, w, h) in board:
            rect = plt.Rectangle((x, y), w, h, fill=None, edgecolor='black')
            ax.add_patch(rect)
            ax.text(x + w/2, y + h/2, f"{w}×{h}", ha='center', va='center', fontsize=8)
        ax.set_aspect('equal')
        ax.invert_yaxis()  # 見やすく上を上に
        figs.append(fig)
        st.pyplot(fig)

        # PNG保存とダウンロードボタン
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        st.download_button(
            label=f"⬇️ 板 {i+1} の図面をダウンロード（PNG）",
            data=buf,
            file_name=f"{title}_board{i+1}.png",
            mime="image/png"
        )
    return figs

# 計算開始
if st.button("板取りを計算"):
    parts_sorted_yield = sorted(parts, key=lambda p: p[0]*p[1], reverse=True)
    parts_sorted_cut = sorted(parts, key=lambda p: p[0], reverse=True)

    boards_yield = pack_rectangles(parts_sorted_yield, rotate=allow_rotate)
    boards_cut = pack_rectangles(parts_sorted_cut, rotate=allow_rotate)

    st.markdown("## 結果")
    st.write(f"① 歩留まり最優先： {len(boards_yield)}枚")
    st.write(f"② カット数最少化： {len(boards_cut)}枚")

    st.subheader("① 歩留まり最優先 配置図")
    draw_board(boards_yield, "歩留まり最優先")

    st.subheader("② カット数最少 配置図")
    draw_board(boards_cut, "カット数最少")
