#!/usr/bin/env python3
"""Trial script to verify OpenAI API connectivity.

Uses the OPENAI_API_KEY environment variable to make a simple
chat completion request and prints the model's response.
"""

import os
import sys

from openai import OpenAI


def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    print("Sending chat completion request...")
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": "What is your name? Reply in one short sentence.",
            }
        ],
        max_tokens=50,
    )

    reply = response.choices[0].message.content
    model_used = response.model
    usage = response.usage

    print(f"Model:  {model_used}")
    print(f"Reply:  {reply}")
    print(f"Tokens: prompt={usage.prompt_tokens}, "
          f"completion={usage.completion_tokens}, "
          f"total={usage.total_tokens}")


if __name__ == "__main__":
    main()
