# Windows: download the local models AMO uses (run from the repo folder).
$chat  = if ($env:AMO_CHAT_MODEL)  { $env:AMO_CHAT_MODEL }  else { "llama3.1:8b" }
$fast  = if ($env:AMO_FAST_MODEL)  { $env:AMO_FAST_MODEL }  else { "llama3.2:3b" }
$embed = if ($env:AMO_EMBED_MODEL) { $env:AMO_EMBED_MODEL } else { "nomic-embed-text" }
$useDocker = (docker compose ps -q ollama 2>$null)
foreach ($m in @($chat, $fast, $embed)) {
  Write-Host "-> pulling $m"
  if ($useDocker) { docker compose exec ollama ollama pull $m } else { ollama pull $m }
}
