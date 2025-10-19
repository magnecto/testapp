import streamlit as st

st.title("🌈 iPhoneから作ったStreamlitアプリ")
st.write("これは **Streamlit Cloud** 上で動いています！")
name = st.text_input("あなたの名前を入力してください")
if name:
    st.success(f"こんにちは、{name}さん！")
