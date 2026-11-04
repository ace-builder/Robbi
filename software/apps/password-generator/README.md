# Password generator

Generate random passwords with selected character groups using operating-system randomness.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

12-128 characters, at most five passwords. Every enabled group appears at least once; passwords are drawn with secrets and rejection sampling. Results may remain in Robbi job/conversation history. This app never writes its own password state and does not replace a password manager.
