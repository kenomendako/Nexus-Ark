-- Nexus Ark API Gateway Roblox relay event client
-- Place this Script under ServerScriptService.
--
-- This version sends events to a relay server instead of sending the real
-- Nexus Ark Bearer Token from Roblox.

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")

local RELAY_BASE_URL = "https://your-relay.example.com"
local RELAY_TOKEN = "paste-your-relay-token"

local EVENT_URL = string.format("%s/events", RELAY_BASE_URL:gsub("/$", ""))

local function postNexusRelayEvent(eventType, summary, details, importance)
	local payload = {
		event_type = eventType,
		source = "roblox_relay",
		trigger_notification = false,
		summary = summary,
		details = details or {},
		importance = importance or "normal",
		attachments = {},
		event_data = details or {},
	}

	local ok, result = pcall(function()
		return HttpService:RequestAsync({
			Url = EVENT_URL,
			Method = "POST",
			Headers = {
				["X-Relay-Token"] = RELAY_TOKEN,
				["Content-Type"] = "application/json",
			},
			Body = HttpService:JSONEncode(payload),
		})
	end)

	if not ok then
		warn("[NexusArkRelay] Event request failed:", result)
		return false
	end

	if not result.Success then
		warn("[NexusArkRelay] Event rejected:", result.StatusCode, result.Body)
		return false
	end

	print("[NexusArkRelay] Event sent:", eventType)
	return true
end

Players.PlayerAdded:Connect(function(player)
	postNexusRelayEvent(
		"roblox_player_joined",
		string.format("%s がRobloxに参加しました", player.Name),
		{
			player_name = player.Name,
			user_id = player.UserId,
		},
		"normal"
	)

	player.Chatted:Connect(function(message)
		postNexusRelayEvent(
			"roblox_player_chat",
			string.format("%s: %s", player.Name, message),
			{
				player_name = player.Name,
				user_id = player.UserId,
				message = message,
			},
			"normal"
		)
	end)
end)
