@echo off
rem Точка входа для планировщика: сводка по заказам без обращения к LLM.
rem Запускается задачей Windows "Orders Agent Summary (logon)" при входе в систему.
cd /d "D:\python projects\openclaw-orders-agent"
python scripts\orders_ops.py summary >> "reports\logon-summary.log" 2>&1
