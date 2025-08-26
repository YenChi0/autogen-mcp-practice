from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Math")

@mcp.tool()
def add(a: float, b: float) -> float: return a + b

@mcp.tool()
def multiply(a: float, b: float) -> float: return a * b

# 👇 新增
@mcp.tool()
def subtract(a: float, b: float) -> float: return a - b

@mcp.tool()
def divide(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("Division by zero")
    return a / b

@mcp.tool()
def mean(nums: list[float]) -> float:
    if not nums:
        raise ValueError("Empty list")
    return sum(nums) / len(nums)

if __name__ == "__main__":
    mcp.run(transport="stdio")
