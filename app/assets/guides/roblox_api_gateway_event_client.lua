-- Nexus Ark API Gateway Roblox event client (local/private use)
-- Place this Script under ServerScriptService.
--
-- Security note:
-- Do not put a real Nexus Ark API token in a public production game. For public
-- games, use api_gateway_token_relay_server.py and
-- roblox_api_gateway_relay_event_client.lua so the Nexus token stays outside Roblox.

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")

local API_BASE_URL = "https://your-domain.example.com"
local ROOM_ID = "Default"
local API_TOKEN = "paste-your-nexus-ark-api-token"

local EVENT_URL = string.format(
	"%s/api/v1/rooms/%s/events",
	API_BASE_URL:gsub("/$", ""),
	HttpService:UrlEncode(ROOM_ID)
)

local function postNexusEvent(eventType, eventData)
	local payload = {
		event_type = eventType,
		event_data = eventData or {},
		trigger_notification = false,
		source = "roblox_api_gateway",
	}

	local ok, result = pcall(function()
		return HttpService:RequestAsync({
			Url = EVENT_URL,
			Method = "POST",
			Headers = {
				["Authorization"] = "Bearer " .. API_TOKEN,
				["Content-Type"] = "application/json",
			},
			Body = HttpService:JSONEncode(payload),
		})
	end)

	if not ok then
		warn("[NexusArk] Event request failed:", result)
		return false
	end

	if not result.Success then
		warn("[NexusArk] Event rejected:", result.StatusCode, result.Body)
		return false
	end

	print("[NexusArk] Event sent:", eventType)
	return true
end

Players.PlayerAdded:Connect(function(player)
	postNexusEvent("roblox_player_joined", {
		player_name = player.Name,
		user_id = player.UserId,
	})

	player.Chatted:Connect(function(message)
		postNexusEvent("roblox_player_chat", {
			player_name = player.Name,
			user_id = player.UserId,
			message = message,
		})
	end)
end)
