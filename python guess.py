import random

secret = random.randint(1, 100)
tries = 0

print("I'm thinking of a number between 1 and 100.")

while True:
    user_input = input("Your guess: ").strip()
    
    # Catch the 'null' or empty inputs
    if not user_input or user_input.lower() == 'null':
        print("The app sent an empty input. Please type a number.")
        continue
        
    try:
        guess = int(user_input)
    except ValueError:
        print("Invalid input! Please enter a valid whole number.")
        continue

    tries += 1

    if guess < secret:
        print("Too low!")
    elif guess > secret:
        print("Too high!")
    else:
        print(f"Correct! You got it in {tries} tries.")
        break