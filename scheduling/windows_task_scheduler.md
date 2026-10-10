# Windows Task Scheduler

Two ways to schedule the daily run on Windows. Times are **local machine time** —
mind the multi-market caveat (see the README / `crontab.example`): one afternoon
run cannot serve both ASX and US. Either run per-market with market-specific
config files, or pick a single time after the last enabled market closes.

Assumes the project is at `C:\opt\stock_analyzer` with a venv at `.venv`.
Adjust paths.

## Option 1: `schtasks` one-liner (one-shot daily run)

Runs `run_daily.py` once per weekday at 22:30 local time:

```bat
schtasks /Create ^
  /TN "StockAnalyzer\DailyRun" ^
  /TR "C:\opt\stock_analyzer\.venv\Scripts\python.exe C:\opt\stock_analyzer\scripts\run_daily.py" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI ^
  /ST 22:30 ^
  /RL LIMITED /F
```

Set the working directory by wrapping in a tiny `.bat` (Task Scheduler's `/TR`
does not take a separate "start in" via `schtasks`):

```bat
:: C:\opt\stock_analyzer\run_daily.bat
@echo off
cd /d C:\opt\stock_analyzer
.venv\Scripts\python.exe scripts\run_daily.py >> logs\task_daily.log 2>&1
```

```bat
schtasks /Create /TN "StockAnalyzer\DailyRun" ^
  /TR "C:\opt\stock_analyzer\run_daily.bat" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 22:30 /F
```

Weekly backtest (Saturdays 02:00):

```bat
schtasks /Create /TN "StockAnalyzer\WeeklyBacktest" ^
  /TR "C:\opt\stock_analyzer\.venv\Scripts\python.exe C:\opt\stock_analyzer\scripts\run_backtest.py" ^
  /SC WEEKLY /D SAT /ST 02:00 /F
```

## Option 2: Import an XML task definition

Save as `StockAnalyzerDaily.xml`, edit the paths, then:
`schtasks /Create /TN "StockAnalyzer\DailyRun" /XML StockAnalyzerDaily.xml`

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Stock Analyzer daily run (after market close)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T22:30:00</StartBoundary>
      <ScheduleByWeek>
        <DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\opt\stock_analyzer\.venv\Scripts\python.exe</Command>
      <Arguments>scripts\run_daily.py</Arguments>
      <WorkingDirectory>C:\opt\stock_analyzer</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

## Option 3: in-process scheduler (no Task Scheduler)

Leave a process running that fires daily at a local time:

```bat
.venv\Scripts\python.exe scripts\run_daily.py --schedule 22:30
```

Verify a task: `schtasks /Query /TN "StockAnalyzer\DailyRun" /V /FO LIST`
Run it now (test): `schtasks /Run /TN "StockAnalyzer\DailyRun"`
