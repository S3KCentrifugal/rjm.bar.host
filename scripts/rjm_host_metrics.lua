-- rjm-bar-host: how far this headless client is behind the server, and why.
--
-- Installed by the lobby client into each game's write dir as
-- LuaUI/Widgets/rjm_host_metrics.lua before the engine starts. BAR runs user
-- widgets for spectators even when a room disallows them
-- (luaui/barwidgets.lua:64-67), and spring-headless runs LuaUI without its
-- draw call-ins (luaui/barwidgets.lua:282-307).
--
-- BAR sandboxes user widgets: they may only write under LuaUI/Config/ or
-- LuaUI/Widgets/ (barwidgets.lua userWritableFolders, checkUserWritePath;
-- game test-31723). The engine's working directory is the write dir
-- (DataDirLocater.cpp ChangeCwdToWriteDir), so these files land in the
-- game's run dir, where the client and the archiver look for them.
--
-- Every EVERY seconds it appends one line to LuaUI/Config/rjm-engine-timeline.csv
-- (a few hundred bytes, flushed; per frame it only compares a timer). Columns:
--   unix_s        wall clock (os.time), to line up with the client's metrics
--   mono_s        seconds since the widget started (Spring.GetTimer)
--   game_frame    this client's sim frame (Spring.GetGameFrame)
--   server_frame  the server's frame, extrapolated from its last
--                 GAME_FRAME_PROGRESS (sent every 150 frames: GameServer.cpp:2736)
--   behind_s      (server_frame - game_frame) / 30: how far behind it is
--   sim_fps       sim frames done per second since the last line
--   ping_s, cpu   what the server reports for this client (PLAYERINFO;
--                 GetPlayerInfo pingTime and cpuUsage)
--   sim_pct, ai_pct
--                 percent of wall time since the last line spent in the engine
--                 timers "Sim" (always recorded) and "AI" (every Skirmish AI
--                 call; recorded only with the profiler on: RJM_PROFILER), from
--                 the timers' running totals, so exact over the interval
--   sim_peak_pct, ai_peak_pct
--                 the engine's own peak one-second share, in percent
--                 (TimeProfiler.cpp UpdateRaw)
--   speed, wanted_speed, lagging
--                 game speed, and Spring.GetGameState's sim-lagging flag
--   units         units alive
--   lua_kb        this LuaUI state's memory
-- LuaUI/Config/rjm-engine-events.csv gets one line per game start and game over.
--
-- With the profiler on, LuaUI/Config/rjm-engine-profile.csv gets, every EVERY
-- seconds, one row per engine timer that ran since the last line:
--   unix_s, game_frame  wall clock and sim frame
--   frames              sim frames since the last line
--   wall_ms             wall time since the last line
--   timer               the engine timer ("Sim", "AI", "Sim::Path", ...)
--   ms                  time in it since the last line
--   ms_per_frame        ms / frames: the budget is 33.3 ms a frame at speed 1
--   wall_pct            ms / wall_ms, in percent
--   max_dt_ms           the engine's longest single call (halved every ~6 s:
--                       TimeProfiler.cpp UpdateRaw)
-- Timers nest: "AI" runs inside "Sim" and overlaps "Sim::Unit::*" (see
-- docs/testing.md). The profiler's own cost is the timer
-- "Misc::Profiler::AddTime".

local EVERY = 5            -- rewritten by the lobby client (BARHOST_METRICS_EVERY)
local RJM_PROFILER = false -- rewritten by the lobby client (BARHOST_ENGINE_PROFILER)

local DIR = "LuaUI/Config/"
local TIMELINE = DIR .. "rjm-engine-timeline.csv"
local EVENTS = DIR .. "rjm-engine-events.csv"
local PROFILE = DIR .. "rjm-engine-profile.csv"
local NAMES_EVERY = 60     -- seconds between refreshes of the timer list

function widget:GetInfo()
	return {
		name = "rjm host metrics",
		desc = "Logs how far this headless host is behind the server, and where its sim time goes (rjm-bar-host)",
		author = "rjm-bar-host",
		date = "2026",
		license = "GPL-2.0-or-later",
		layer = -100000,
		enabled = true,
	}
end

local GetTimer, DiffTimers = Spring.GetTimer, Spring.DiffTimers
local GetGameFrame = Spring.GetGameFrame
local GetTimeRecord = Spring.GetProfilerTimeRecord
local format, concat = string.format, table.concat

local out, events, prof
local started
local lastSample, lastSampleFrame
local serverFrame, serverFrameAt = nil, nil
local lastTotal = {}       -- timer name -> its running total (ms) at the last line
local names = {}           -- the engine's timer names (profiler on)
local namesAt
local measured = {}        -- "Sim" / "AI" -> {ms, max_dt} of this line, for the profile rows

local function num(v, fmt)
	if v == nil then return "" end
	if type(v) == "boolean" then return v and "1" or "0" end
	return format(fmt or "%.3f", v)
end

-- Time in a timer since the last line (ms), from its running total; also its
-- longest call and peak share. GetProfilerTimeRecord gives total ms, current
-- ms, max-dt, time share, peak share (LuaUnsyncedRead.cpp); no frame data,
-- which builds a table every call.
local function since(name)
	local ok, total, _, maxdt, _, peak = pcall(GetTimeRecord, name)
	if not ok or type(total) ~= "number" then return nil end
	local d = total - (lastTotal[name] or total)
	lastTotal[name] = total
	return d, maxdt, peak
end

local function speeds()
	local ok, wanted, speed = pcall(Spring.GetGameSpeed)
	if ok then return wanted, speed end
	return nil, nil
end

local function event(what, detail)
	if events then
		events:write(format("%d,%d,%s,%s\n", os.time(), GetGameFrame(), what, detail or ""))
		events:flush()
	end
end

local function refreshNames(now)
	local ok, list = pcall(Spring.GetProfilerRecordNames)
	if ok and type(list) == "table" then names = list end
	namesAt = now
end

-- Open for appending; on failure say why (the sandbox gives its reason).
local function open(path)
	local f, why = io.open(path, "a")
	if not f then
		Spring.Echo("rjm host metrics: cannot write " .. path .. ": " .. tostring(why))
	end
	return f
end

function widget:Initialize()
	out = open(TIMELINE)
	if not out then
		Spring.Echo("rjm host metrics: off")
		widgetHandler:RemoveWidget(self)
		return
	end
	events = open(EVENTS)
	out:write("unix_s,mono_s,game_frame,server_frame,behind_s,sim_fps,ping_s,cpu,sim_pct,sim_peak_pct,"
		.. "ai_pct,ai_peak_pct,speed,wanted_speed,lagging,units,lua_kb\n")
	out:flush()
	if events then events:write("unix_s,game_frame,event,detail\n") end
	started = GetTimer()
	lastSample, lastSampleFrame = started, GetGameFrame()
	if RJM_PROFILER then
		Spring.SendCommands("debug 1 0")   -- profiler on, no drawing (UnsyncedGameCommands.cpp DebugActionExecutor)
		prof = open(PROFILE)
		if prof then
			prof:write("unix_s,game_frame,frames,wall_ms,timer,ms,ms_per_frame,wall_pct,max_dt_ms\n")
			prof:flush()
		end
		refreshNames(started)
		for i = 1, #names do since(names[i]) end   -- the first line covers its interval only
	end
	since("Sim")
	since("AI")
	event("widget_start", RJM_PROFILER and "profiler" or "")
end

function widget:GameProgress(frame)
	serverFrame, serverFrameAt = frame, GetTimer()
end

function widget:GameStart()
	event("game_start", "")
end

function widget:GameOver(winners)
	local w = {}
	for _, a in ipairs(winners or {}) do w[#w + 1] = tostring(a) end
	event("game_over", concat(w, " "))
end

-- One row per timer that ran since the last line, written at once.
local function profileSample(now, frame, frames, wallMs)
	if DiffTimers(now, namesAt) >= NAMES_EVERY then refreshNames(now) end
	local rows, t = {}, os.time()
	for i = 1, #names do
		local name = names[i]
		local d, maxdt
		local m = measured[name]       -- "Sim" and "AI": read for the timeline line already
		if m then
			d, maxdt = m[1], m[2]
		else
			d, maxdt = since(name)
		end
		if d and d > 0 then
			rows[#rows + 1] = format("%d,%d,%d,%.0f,%s,%.2f,%.3f,%.2f,%.2f", t, frame, frames, wallMs,
				name, d, frames > 0 and d / frames or 0, wallMs > 0 and d * 100 / wallMs or 0, maxdt or 0)
		end
	end
	if rows[1] then
		prof:write(concat(rows, "\n"), "\n")
		prof:flush()
	end
end

local function sample(now)
	local frame = GetGameFrame()
	local elapsed = DiffTimers(now, lastSample)
	local wallMs = elapsed * 1000
	local wanted, speed = speeds()
	local server
	if serverFrame then
		server = serverFrame + DiffTimers(now, serverFrameAt) * 30 * (speed or 1)
	end
	local behind = server and math.max(0, (server - frame) / 30) or nil
	local _, _, _, _, _, ping, cpu = Spring.GetPlayerInfo(Spring.GetMyPlayerID(), false)
	local simMs, simMax, simPeak = since("Sim")
	local aiMs, aiMax, aiPeak = since("AI")
	measured.Sim, measured.AI = { simMs, simMax }, { aiMs, aiMax }
	local function pct(ms) return (ms and wallMs > 0) and ms * 100 / wallMs or nil end
	local lagging = nil
	-- doneLoading, isSavedGame, isClientPaused, isSimLagging (LuaUnsyncedRead.cpp GetGameState)
	local ok, _, _, _, isLagging = pcall(Spring.GetGameState)
	if ok then lagging = isLagging end
	local units = #Spring.GetAllUnits()
	out:write(concat({
		tostring(os.time()), num(DiffTimers(now, started), "%.1f"), tostring(frame),
		server and format("%d", server) or "", num(behind, "%.2f"),
		num(elapsed > 0 and (frame - lastSampleFrame) / elapsed or nil, "%.1f"),
		num(ping, "%.3f"), num(cpu, "%.3f"), num(pct(simMs), "%.2f"), num(simPeak and simPeak * 100, "%.2f"),
		num(pct(aiMs), "%.2f"), num(aiPeak and aiPeak * 100, "%.2f"), num(speed, "%.2f"), num(wanted, "%.2f"),
		num(lagging), tostring(units), num(collectgarbage("count"), "%.0f"),
	}, ",") .. "\n")
	out:flush()
	if prof then
		profileSample(now, frame, frame - lastSampleFrame, wallMs)
	end
	lastSample, lastSampleFrame = now, frame
end

function widget:Update()
	if not out then return end
	local now = GetTimer()
	if DiffTimers(now, lastSample) >= EVERY then
		sample(now)
	end
end

function widget:Shutdown()
	event("widget_stop", "")
	if out then out:close(); out = nil end
	if events then events:close(); events = nil end
	if prof then prof:close(); prof = nil end
end
