from directory_transfer import directory_transfer
from metadata_enrichment import metadata_enrichment
from metadata_moving import metadata_moving

def main():
  directory_transfer()
  metadata_enrichment()
  metadata_moving()

if __name__ == "__main__":
  main()