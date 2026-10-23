-- rjm-bar-host: how far this headless client is behind the server, and why.
--
-- Installed by the lobby client into each game's write dir as
-- LuaUI/Widgets/rjm_host_metrics.lua before the engine starts. BAR runs user
-- widgets for spectators even when a room disallows them
-- (luaui/barwidgets.lua:64-67), and spring-headless runs LuaUI without its
-- draw call-ins (luaui/barwidgets.lua:282-307).
--
-- Every EVERY seconds it appends one line to engine-timeline.csv in the write
-- dir (a few hundred bytes, flushed; nothing else is done per frame beyond
-- counting). Columns:
--   unix_s        wall clock (os.time), to line up with the client's metrics
--   mono_s        seconds since the widget started (Spring.GetTimer)
--   game_frame    this client's sim frame (Spring.GetGameFrame)
--   server_frame  the server's frame, extrapolated from its last
--                 GAME_FRAME_PROGRESS (sent every 150 frames: GameServer.cpp:2736)
--   behind_s      (server_frame - game_frame) / 30: how far behind it is
--   sim_fps       sim frames done per second since the last line
--   ping_s, cpu   what the server reports for this client (PLAYERINFO;
--                 GetPlayerInfo pingTime and cpuUsage)
--   sim_pct, sim_peak_pct, ai_pct, ai_peak_pct
--                 the engine profiler's share of time in "Sim" (always
--                 recorded) and "AI" (all Skirmish AI calls; recorded only
--                 when the profiler is on: RJM_PROFILER below)
--   speed, wanted_speed, lagging
--                 game speed, and Spring.GetGameState's sim-lagging flag
--   units         units alive
--   lua_kb        this LuaUI state's memory
-- engine-events.csv gets one line per game start and game over.

local EVERY = 5            -- rewritten by the lobby client (BARHOST_METRICS_EVERY)
local RJM_PROFILER = false -- rewritten by the lobby client (BARHOST_ENGINE_PROFILER)

function widget:GetInfo()
	return {
		name = "rjm host metrics",
		desc = "Logs how far this headless host is behind the server (rjm-bar-host)",
		author = "rjm-bar-host",
		date = "2026",
		license = "GPL-2.0-or-later",
		layer = -100000,
		enabled = true,
	}
end

local GetTimer, DiffTimers = Spring.GetTimer, Spring.DiffTimers
local GetGameFrame = Spring.GetGameFrame

local out, events
local started
local lastSample, lastSampleFrame
local serverFrame, serverFrameAt = nil, nil

local function num(v, fmt)
	if v == nil then return "" end
	if type(v) == "boolean" then return v and "1" or "0" end
	return string.format(fmt or "%.3f", v)
end

-- total ms, current ms, max-dt, time share, peak share (LuaUnsyncedRead.cpp:621-625),
-- logged as the engine gives them.
local function profiler(name)
	local ok, _, _, _, timePct, peakPct = pcall(Spring.GetProfilerTimeRecord, name)
	if not ok then return nil, nil end
	return timePct, peakPct
end

local function speeds()
	local ok, wanted, speed = pcall(Spring.GetGameSpeed)
	if ok then return wanted, speed end
	return nil, nil
end

local function event(what, detail)
	if events then
		events:write(string.format("%d,%d,%s,%s\n", os.time(), GetGameFrame(), what, detail or ""))
		events:flush()
	end
end

function widget:Initialize()
	out = io.open("engine-timeline.csv", "a")
	events = io.open("engine-events.csv", "a")
	if not out then
		Spring.Echo("rjm host metrics: cannot write engine-timeline.csv; off")
		widgetHandler:RemoveWidget(self)
		return
	end
	out:write("unix_s,mono_s,game_frame,server_frame,behind_s,sim_fps,ping_s,cpu,sim_pct,sim_peak_pct,"
		.. "ai_pct,ai_peak_pct,speed,wanted_speed,lagging,units,lua_kb\n")
	out:flush()
	if events then events:write("unix_s,game_frame,event,detail\n") end
	started = GetTimer()
	lastSample, lastSampleFrame = started, GetGameFrame()
	if RJM_PROFILER then
		Spring.SendCommands("debug 1 0")   -- profiler on, no drawing (UnsyncedGameCommands.cpp:1530)
	end
	event("widget_start", "")
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
	event("game_over", table.concat(w, " "))
end

local function sample(now)
	local frame = GetGameFrame()
	local elapsed = DiffTimers(now, lastSample)
	local wanted, speed = speeds()
	local server
	if serverFrame then
		server = serverFrame + DiffTimers(now, serverFrameAt) * 30 * (speed or 1)
	end
	local behind = server and math.max(0, (server - frame) / 30) or nil
	local _, _, _, _, _, ping, cpu = Spring.GetPlayerInfo(Spring.GetMyPlayerID(), false)
	local simPct, simPeak = profiler("Sim")
	local aiPct, aiPeak = profiler("AI")
	local lagging = nil
	-- doneLoading, isSavedGame, isClientPaused, isSimLagging (LuaUnsyncedRead.cpp:3683-3687)
	local ok, _, _, _, isLagging = pcall(Spring.GetGameState)
	if ok then lagging = isLagging end
	local units = #Spring.GetAllUnits()
	out:write(table.concat({
		tostring(os.time()), num(DiffTimers(now, started), "%.1f"), tostring(frame),
		server and string.format("%d", server) or "", num(behind, "%.2f"),
		num(elapsed > 0 and (frame - lastSampleFrame) / elapsed or nil, "%.1f"),
		num(ping, "%.3f"), num(cpu, "%.3f"), num(simPct, "%.2f"), num(simPeak, "%.2f"),
		num(aiPct, "%.2f"), num(aiPeak, "%.2f"), num(speed, "%.2f"), num(wanted, "%.2f"),
		num(lagging), tostring(units), num(collectgarbage("count"), "%.0f"),
	}, ",") .. "\n")
	out:flush()
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
end
