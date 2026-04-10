#!/bin/bash
# Lily Bot Setup Script for GCP Ubuntu VM

echo "🚀 Starting Lily Bot Setup..."

# 1. Update system
sudo apt update && sudo apt upgrade -y

# 2. Install Python and Pip
sudo apt install python3 python3-pip git -y

# 3. Clone Repository (if not already cloned)
if [ ! -d "YT-livechat-parser" ]; then
    echo "📂 Cloning repository..."
    # Replace the URL with your real repo URL if needed
    git clone https://github.com/Lyfora/YT-livechat-parser.git
fi

cd YT-livechat-parser

# 4. Install Dependencies
echo "📦 Installing Python dependencies..."
pip3 install -r requirements.txt

echo "✅ Setup complete!"
echo "⚠️ IMPORTANT: Now manually upload your .env and token.pickle to this folder."
echo "⚠️ AFTER UPLOADING: Run 'sudo cp lilybot.service /etc/systemd/system/'"
echo "⚠️ THEN: 'sudo systemctl enable lilybot && sudo systemctl start lilybot'"
