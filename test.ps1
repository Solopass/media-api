# Simulate a Media App using the YouTube API Service

Write-Host "1. Starting the API Service..."
# Start the API in a hidden background process
$apiProcess = Start-Process pwsh -ArgumentList "-File .\YouTubeAPI.ps1 -Port 8085" -WindowStyle Hidden -PassThru

# Give the API a second to spin up
Start-Sleep -Seconds 2

try {
    Write-Host "2. Requesting the API to download a video..."
    # We'll use a very short test video: "Me at the zoo" (the first YT video)
    $response = Invoke-RestMethod -Uri "http://localhost:8085/download?url=https://www.youtube.com/watch?v=jNQXAC9IVRw&format=mp4"
    $jobId = $response.Id
    
    Write-Host "   -> Job ID received: $jobId"
    Write-Host "3. Polling the API for status..."

    $isDone = $false
    while (-not $isDone) {
        Start-Sleep -Seconds 2
        $status = Invoke-RestMethod -Uri "http://localhost:8085/status?id=$jobId"
        Write-Host "   -> Status: $($status.Status)"
        
        if ($status.Status -eq "completed") {
            Write-Host "   -> SUCCESS! File downloaded to: $($status.File)" -ForegroundColor Green
            $isDone = $true
        } elseif ($status.Status -eq "failed") {
            Write-Host "   -> FAILED! Error: $($status.Error)" -ForegroundColor Red
            $isDone = $true
        }
    }
} finally {
    Write-Host "4. Shutting down the API Service..."
    # The app is done, so we kill the API process to save resources
    Stop-Process -Id $apiProcess.Id -Force
    Write-Host "Service stopped."
}
