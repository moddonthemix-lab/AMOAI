# Windows: download the local models Modd uses (run from the repo folder).
$chat  = if ($env:MODD_CHAT_MODEL)  { $env:MODD_CHAT_MODEL }  else { "llama3.1:8b" }
$fast  = if ($env:MODD_FAST_MODEL)  { $env:MODD_FAST_MODEL }  else { "llama3.2:3b" }
$embed = if ($env:MODD_EMBED_MODEL) { $env:MODD_EMBED_MODEL } else { "nomic-embed-text" }
$useDocker = (docker compose ps -q ollama 2>$null)
foreach ($m in @($chat, $fast, $embed)) {
  Write-Host "-> pulling $m"
  if ($useDocker) { docker compose exec ollama ollama pull $m } else { ollama pull $m }
}
