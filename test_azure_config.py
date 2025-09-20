#!/usr/bin/env python3
"""
Test script to verify Azure OpenAI configuration
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain.schema import HumanMessage

def test_azure_config():
    # Load secrets
    secrets_file = Path(".secrets")
    if secrets_file.exists():
        load_dotenv(secrets_file)
        print("✅ Loaded .secrets file")
    else:
        print("❌ No .secrets file found")
        return False

    # Check configuration
    required_vars = ["AZURE_ENDPOINT", "AZURE_SUBSCRIPTION_KEY", "AZURE_API_VERSION", "AZURE_DEPLOYMENT"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        print(f"❌ Missing required environment variables: {missing_vars}")
        return False

    print("✅ All Azure OpenAI environment variables present")

    # Test configuration values
    print(f"🔧 AZURE_ENDPOINT: {os.getenv('AZURE_ENDPOINT')}")
    print(f"🔧 AZURE_DEPLOYMENT: {os.getenv('AZURE_DEPLOYMENT')}")
    print(f"🔧 AZURE_API_VERSION: {os.getenv('AZURE_API_VERSION')}")
    print(f"🔧 AZURE_SUBSCRIPTION_KEY: {os.getenv('AZURE_SUBSCRIPTION_KEY')[:10]}...{os.getenv('AZURE_SUBSCRIPTION_KEY')[-10:]}")

    # Try to create LLM instance
    try:
        llm = AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_ENDPOINT"),
            azure_deployment=os.getenv("AZURE_DEPLOYMENT"),
            api_key=os.getenv("AZURE_SUBSCRIPTION_KEY"),
            api_version=os.getenv("AZURE_API_VERSION"),
            temperature=0.3,
            max_tokens=100
        )
        print("✅ Azure OpenAI LLM instance created successfully")

        # Test a simple call
        print("🧪 Testing simple API call...")
        response = llm.invoke([HumanMessage(content="Say 'Hello from Azure OpenAI!' in exactly 5 words.")])
        print(f"✅ API Response: {response.content}")

        return True

    except Exception as e:
        print(f"❌ Failed to create or test Azure OpenAI LLM: {str(e)}")
        return False

if __name__ == "__main__":
    print("🔍 Testing Azure OpenAI Configuration...")
    success = test_azure_config()

    if success:
        print("\n🎉 Azure OpenAI configuration is working correctly!")
    else:
        print("\n💥 Azure OpenAI configuration has issues. Please check your credentials.")

    exit(0 if success else 1)