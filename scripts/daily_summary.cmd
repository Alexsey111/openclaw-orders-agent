@echo off
rem Обёртка для ручного вызова сводки с записью в журнал.
rem Задача Windows "Orders Agent Summary (logon)" снята 2026-11-02: сводке нужно
rem поднятое ядро, а при входе в систему его никто не поднимает (DEF-021).
cd /d "D:\python projects\openclaw-orders-agent"
python scripts\orders_ops.py summary >> "reports\logon-summary.log" 2>&1
