"""Prime numbers."""


def is_prime(number):
    """Whether number is a prime."""
    # numbers below two are not prime
    if number < 2:
        return False

    # try every possible divisor up to the square root
    divisor = 2
    while divisor * divisor <= number:

        # a divisor without remainder means it is not prime
        if number % divisor == 0:
            return False

        divisor = divisor + 1

    # no divisor was found
    return True
