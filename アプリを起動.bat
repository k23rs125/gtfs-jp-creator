@echo off
rem === GTFS-JP メーカー 起動（ダブルクリックで実行）=======================
rem このファイルをダブルクリックすると、背景地図つきでアプリが起動します。
rem ブラウザが自動で http://localhost:8534 を開きます。
rem 閉域網（インターネット無し）で使う場合は、下の set GTFS_TILE_URL 行の
rem 先頭に rem を付けて無効化してください（背景地図なしで動きます）。
rem ========================================================================
cd /d "%~dp0"
set "GTFS_TILE_URL=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
python -m streamlit run app/app.py
pause
