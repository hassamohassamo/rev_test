def sumup(a: int, b: int) -> int:
    """Return the sum of two numbers.
    
    Args:
        a (int): First number.
        b (int): Second number.
    
    Returns:
        int: The sum of `a` and `b`.
    """
    result = a + b
    return result


def multiply(a: int, b: int) -> int:
    """Return the product of two numbers.
    
    Args:
        a (int): First number.
        b (int): Second number.
    
    Returns:
        int: The product of `a` and `b`.
    """
    result = a * b
    return result

print(sumup(3, 4))
print(multiply(3, 4))